# frozen_string_literal: true

require "json"
require "fileutils"
require "rack"
require "stringio"
require_relative "web_agent_route_token"
require_relative "web_agent_route_registry"
require_relative "web_agent_connection_manager"

module WebAgentIsolation
  module CurrentEnvironment
    THREAD_KEY = :web_agent_environment_context

    def self.current
      Thread.current[THREAD_KEY]
    end

    def self.fetch(key)
      (current || {}).fetch(key)
    end

    def self.redis_cache_prefix
      fetch("redis_cache_prefix")
    end

    def self.redis_state_prefix
      fetch("redis_state_prefix")
    end

    def self.opensearch_index
      fetch("opensearch_index")
    end

    def self.with(route)
      previous = Thread.current[THREAD_KEY]
      Thread.current[THREAD_KEY] = route.freeze
      yield
    ensure
      Thread.current[THREAD_KEY] = previous
    end
  end

  class DatabaseRouter
    def initialize(app, token_verifier:, registry:, connections:, probe_enabled: false)
      @app = app
      @token_verifier = token_verifier
      @registry = registry
      @connections = connections
      @probe_enabled = probe_enabled
    end

    def call(env)
      return health_response if public_health_path?(env["PATH_INFO"])
      return @app.call(env) if env["PATH_INFO"] == "/help"

      agent_id = resolve_agent_id(env)
      if env["PATH_INFO"] == "/__web_agent_release_pool"
        return release_pool_response(env, agent_id)
      end
      route = @registry.acquire(agent_id)
      leased = true
      strip_route_headers(env)
      env["web_agent.agent_id"] = agent_id
      env["web_agent.environment"] = route
      env["web_agent.database_name"] = route.fetch("db_name")
      env["web_agent.state_namespace"] = route.fetch("state_namespace")
      env["web_agent.redis_cache_prefix"] = route.fetch("redis_cache_prefix")
      env["web_agent.redis_state_prefix"] = route.fetch("redis_state_prefix")
      env["web_agent.opensearch_index"] = route.fetch("opensearch_index")
      CurrentEnvironment.with(route) do
        @connections.with_route(route) do
          if @probe_enabled && env["PATH_INFO"].start_with?("/__web_agent_probe")
            probe_response(env, route, agent_id)
          elsif env["PATH_INFO"].start_with?("/__web_agent_state_file/")
            serve_state_file(env, route)
          else
            @app.call(env)
          end
        end
      end
    rescue WebAgentIsolation::RouteFrozen => error
      [503, { "content-type" => "text/plain", "retry-after" => "1" }, [error.message]]
    rescue WebAgentIsolation::InvalidRouteToken, KeyError => error
      if @probe_enabled && env["PATH_INFO"].to_s.start_with?("/api/v4/internal/")
        warn(
          "WebAgent internal callback routing failed: #{error.message}; " \
          "remote=#{env['REMOTE_ADDR'].inspect}; " \
          "payload=#{env['web_agent.internal_callback_payload'].inspect}"
        )
      end
      [403, { "content-type" => "text/plain" }, [error.message]]
    rescue StandardError => error
      raise unless @probe_enabled && env["PATH_INFO"] == "/__web_agent_probe"

      body = JSON.generate(
        error: error.class.name,
        message: error.message,
        backtrace: error.backtrace&.first(5)
      )
      [500, { "content-type" => "application/json", "content-length" => body.bytesize.to_s }, [body]]
    ensure
      @registry.release(agent_id) if defined?(leased) && leased
    end

    private

    def public_health_path?(path)
      path == "/-/health" || path == "/-/readiness" || path == "/-/liveness"
    end

    def health_response
      body = "GitLab OK"
      [200, { "content-type" => "text/plain", "content-length" => body.bytesize.to_s }, [body]]
    end

    def release_pool_response(env, agent_id)
      unless env["REQUEST_METHOD"] == "POST"
        return [405, { "allow" => "POST", "content-type" => "text/plain" }, ["method not allowed"]]
      end

      route = @registry.route_for(agent_id)
      strip_route_headers(env)
      released = @connections.release(route.fetch("pool_key"))
      json_response(
        200,
        agent_id: agent_id,
        pool_key: route.fetch("pool_key"),
        released: released,
        process_id: Process.pid,
        registered_pool_count: @connections.registered_pool_count
      )
    end

    def probe_response(env, route, agent_id)
      if env["PATH_INFO"] == "/__web_agent_probe/pool"
        return json_response(
          200,
          agent_id: agent_id,
          process_id: Process.pid,
          registered_pool_count: @connections.registered_pool_count
        )
      end
      if env["PATH_INFO"] == "/__web_agent_probe/sidekiq"
        return [405, { "allow" => "POST", "content-type" => "text/plain" }, ["method not allowed"]] unless env["REQUEST_METHOD"] == "POST"

        jid = WebAgentIsolation::SidekiqRouteProbeWorker.perform_async(agent_id)
        return json_response(202, jid: jid, agent_id: agent_id)
      end
      if env["PATH_INFO"] == "/__web_agent_probe/artifact"
        return [405, { "allow" => "POST", "content-type" => "text/plain" }, ["method not allowed"]] unless env["REQUEST_METHOD"] == "POST"

        relative = File.join("web-agent-probe", "#{agent_id}.txt")
        target = File.join(JobArtifactUploader.root, relative)
        FileUtils.mkdir_p(File.dirname(target))
        File.write(target, agent_id)
        return json_response(
          201,
          agent_id: agent_id,
          url: "/__web_agent_state_file/artifacts/#{relative}"
        )
      end
      raise KeyError, "unknown WebAgent probe" unless env["PATH_INFO"] == "/__web_agent_probe"

      redis_previous = {
        shared_state: redis_probe(Gitlab::Redis::SharedState, "web-agent-probe", agent_id),
        cache: redis_probe(Gitlab::Redis::Cache, "web-agent-probe-raw-cache", agent_id),
        sessions: redis_probe(Gitlab::Redis::Sessions, "web-agent-probe", agent_id),
        trace_chunks: redis_probe(Gitlab::Redis::TraceChunks, "web-agent-probe", agent_id)
      }
      rate_store = Gitlab::Redis::RateLimiting.cache_store
      redis_previous[:rate_limiting] = rate_store.read("web-agent-probe-rate-limit")
      rate_store.write("web-agent-probe-rate-limit", agent_id, expires_in: 60)
      redis_previous[:rails_cache] = Rails.cache.read("web-agent-probe-rails-cache")
      Rails.cache.write("web-agent-probe-rails-cache", agent_id, expires_in: 60)
      sidekiq_value = Gitlab::Redis::SharedState.with do |redis|
        redis.get("web-agent-sidekiq-probe")
      end
      settings = ApplicationSetting.current

      json_response(
        200,
        agent_id: agent_id,
        environment_id: route.fetch("environment_id"),
        branch_id: route.fetch("branch_id"),
        generation: route.fetch("generation"),
        expected_database: route.fetch("db_name"),
        database: ApplicationRecord.connection.select_value("SELECT current_database()"),
        shard: ApplicationRecord.current_shard,
        process_id: Process.pid,
        registered_pool_count: @connections.registered_pool_count,
        redis_state_prefix: route.fetch("redis_state_prefix"),
        redis_cache_prefix: route.fetch("redis_cache_prefix"),
        redis_previous: redis_previous,
        rails_cache_store: Rails.cache.class.name,
        sidekiq_value: sidekiq_value,
        uploads_root: GitlabUploader.root,
        uploads_component_root: route.fetch("uploads_root"),
        file_uploads_root: FileUploader.root,
        artifacts_root: JobArtifactUploader.root,
        gitaly_relative_prefix: route.fetch("gitaly_relative_prefix"),
        elasticsearch_search: settings.elasticsearch_search,
        elasticsearch_indexing: settings.elasticsearch_indexing
      )
    end

    def redis_probe(wrapper, key, agent_id)
      wrapper.with do |redis|
        previous = redis.get(key)
        redis.set(key, agent_id, ex: 60)
        previous
      end
    end

    def json_response(status, payload)
      body = JSON.generate(payload)
      [status, { "content-type" => "application/json", "content-length" => body.bytesize.to_s }, [body]]
    end

    def serve_state_file(env, route)
      match = env["PATH_INFO"].match(%r{\A/__web_agent_state_file/(uploads|artifacts)/(.*)\z})
      return [404, { "content-type" => "text/plain" }, ["not found"]] unless match

      root_key = match[1] == "uploads" ? "uploads_root" : "artifacts_root"
      root = File.expand_path(route.fetch(root_key))
      relative = match[2]
      target = File.expand_path(relative, root)
      unless target.start_with?("#{root}/") && File.file?(target)
        return [404, { "content-type" => "text/plain" }, ["not found"]]
      end

      file_env = env.merge("PATH_INFO" => "/#{relative}")
      Rack::Files.new(root).call(file_env)
    end

    def resolve_agent_id(env)
      token = env["HTTP_X_AGENT_ROUTE"]
      return @token_verifier.verify!(token) if token

      recover_internal_agent_id(env)
    end

    def recover_internal_agent_id(env)
      unless env["PATH_INFO"].start_with?("/api/v4/internal/") &&
          ["127.0.0.1", "::1"].include?(env["REMOTE_ADDR"])
        raise KeyError, "missing WebAgent route"
      end

      input = env.fetch("rack.input")
      raw = input.read
      input.rewind
      env["web_agent.internal_callback_payload"] = raw
      payload = JSON.parse(raw)
      key = "gl_repository"
      context_match = payload[key].to_s.match(
        /\Awebagent:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):g([0-9]+):(.+)\z/
      )
      if context_match
        agent_id, branch_id, generation, repository = context_match.captures
      else
        project_path = payload["project"].to_s
        path_match = project_path.match(
          %r{/\.web-agent/environments/([A-Za-z0-9_.-]+)/branches/([A-Za-z0-9_.-]+)/gitaly/}
        )
        raise KeyError, "internal callback has no route context" unless path_match

        agent_id, branch_id = path_match.captures
        generation = nil
        repository = nil
      end
      route = @registry.route_for(agent_id)
      unless route.fetch("environment_id") == agent_id &&
          route.fetch("branch_id") == branch_id &&
          (generation.nil? || route.fetch("generation").to_i == generation.to_i)
        raise KeyError, "stale internal callback route"
      end

      if repository
        payload[key] = repository
        if payload["env"]
          hook_env = JSON.parse(payload["env"])
          Gitlab::Git::HookEnv.set(payload[key], hook_env)
          Gitlab::Git::HookEnv.set(
            "webagent:#{agent_id}:#{branch_id}:g#{generation}:#{repository}",
            hook_env
          )
        end
        rewritten = JSON.generate(payload)
        env["rack.input"] = StringIO.new(rewritten)
        env["CONTENT_LENGTH"] = rewritten.bytesize.to_s
      end
      agent_id
    rescue JSON::ParserError
      raise KeyError, "invalid internal callback payload"
    end

    def strip_route_headers(env)
      env.delete("HTTP_X_AGENT_ROUTE")
      env.delete("HTTP_X_AGENT_ID")
      env.delete("HTTP_X_AGENT_DB")
    end
  end
end
