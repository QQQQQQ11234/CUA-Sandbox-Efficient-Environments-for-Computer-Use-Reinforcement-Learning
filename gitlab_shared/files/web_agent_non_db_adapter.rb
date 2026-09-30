# frozen_string_literal: true

require "redis-namespace"

module WebAgentIsolation
  module RoutedUploaderRoot
    def root
      WebAgentIsolation::RoutedUploaderRoot.route(super)
    end

    def self.route(original)
      route = WebAgentIsolation::CurrentEnvironment.current
      return original unless route

      uploads_storage = Gitlab.config.uploads.storage_path.to_s
      uploads_tree = File.join(uploads_storage, "uploads")
      if original.to_s == uploads_storage
        # GitLab's upload root is `public`, while the snapshotted component is
        # the contents of `public/uploads`. Return the branch directory so the
        # configured `uploads/...` base_dir keeps the same relative layout.
        File.dirname(route.fetch("uploads_root"))
      elsif original.to_s == uploads_tree
        route.fetch("uploads_root")
      elsif original.to_s.start_with?("#{uploads_tree}/")
        File.join(route.fetch("uploads_root"), original.to_s.delete_prefix("#{uploads_tree}/"))
      elsif original.to_s == Gitlab.config.artifacts.storage_path.to_s
        route.fetch("artifacts_root")
      else
        original
      end
    end
  end

  module RoutedUploaderInstance
    def local_url
      original = super
      route = WebAgentIsolation::CurrentEnvironment.current
      return original unless route

      storage_path = self.class.options.storage_path.to_s
      component =
        if storage_path == Gitlab.config.uploads.storage_path.to_s
          "uploads"
        elsif storage_path == Gitlab.config.artifacts.storage_path.to_s
          "artifacts"
        end
      return original unless component

      relative = original.sub(%r{\A/+}, "")
      relative = relative.sub(%r{\Auploads/}, "") if component == "uploads"
      "/__web_agent_state_file/#{component}/#{relative}"
    end
  end

  module RoutedGitalyRepository
    def repository(repository_storage, relative_path, gl_repository, gl_project_path)
      route = WebAgentIsolation::CurrentEnvironment.current
      if route
        prefix = route.fetch("gitaly_relative_prefix")
        unless relative_path.to_s.empty? || relative_path.start_with?("#{prefix}/")
          relative_path = File.join(prefix, relative_path)
        end
        unless gl_repository.to_s.empty? || gl_repository.start_with?("webagent:")
          gl_repository = [
            "webagent",
            route.fetch("environment_id"),
            route.fetch("branch_id"),
            "g#{route.fetch('generation')}",
            gl_repository
          ].join(":")
        end
      end
      super(repository_storage, relative_path, gl_repository, gl_project_path)
    end
  end

  module RoutedHookEnv
    ROUTED_REPOSITORY = /\Awebagent:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:g[0-9]+:(.+)\z/

    def all(gl_repository)
      match = gl_repository.to_s.match(ROUTED_REPOSITORY)
      super(match ? match[1] : gl_repository)
    end
  end

  module RoutedRedis
    module_function

    def cache_namespace
      route = WebAgentIsolation::CurrentEnvironment.current
      route ? "#{route.fetch('redis_cache_prefix')}gitlab" : Gitlab::Redis::Cache::CACHE_NAMESPACE
    end

    def state_namespace(default_namespace)
      route = WebAgentIsolation::CurrentEnvironment.current
      route ? "#{route.fetch('redis_state_prefix')}#{default_namespace}" : default_namespace
    end

    def wrap(redis, prefix)
      Redis::Namespace.new(-> { prefix.call }, redis: redis, warning: false)
    end
  end

  module RoutedSharedStateRedis
    def with
      super do |redis|
        wrapped = WebAgentIsolation::RoutedRedis.wrap(
          redis,
          -> { WebAgentIsolation::RoutedRedis.state_namespace("shared_state:gitlab") }
        )
        yield wrapped
      end
    end
  end

  module RoutedCacheRedis
    def with
      super do |redis|
        wrapped = WebAgentIsolation::RoutedRedis.wrap(
          redis,
          -> { WebAgentIsolation::RoutedRedis.cache_namespace }
        )
        yield wrapped
      end
    end
  end

  module RoutedSessionsRedis
    def with
      super do |redis|
        wrapped = WebAgentIsolation::RoutedRedis.wrap(
          redis,
          -> { WebAgentIsolation::RoutedRedis.state_namespace(Gitlab::Redis::Sessions::SESSION_NAMESPACE) }
        )
        yield wrapped
      end
    end
  end

  module RoutedTraceChunksRedis
    def with
      super do |redis|
        wrapped = WebAgentIsolation::RoutedRedis.wrap(
          redis,
          -> { WebAgentIsolation::RoutedRedis.state_namespace("trace_chunks:gitlab") }
        )
        yield wrapped
      end
    end
  end

  module RoutedRateLimitingRedis
    def cache_store
      @web_agent_cache_store ||= ActiveSupport::Cache::RedisCacheStore.new(
        redis: pool,
        namespace: -> { WebAgentIsolation::RoutedRedis.cache_namespace }
      )
    end
  end

  class SidekiqRouteProbeWorker
    include Sidekiq::Worker

    sidekiq_options retry: false, queue: :default

    def perform(value)
      Gitlab::Redis::SharedState.with do |redis|
        redis.set("web-agent-sidekiq-probe", value, ex: 300)
      end
    end
  end

  class SidekiqClientRouteMiddleware
    ROUTE_KEY = "web_agent_route"

    def initialize(registry)
      @registry = registry
    end

    def call(_worker_class, job, _queue, _redis_pool)
      route = WebAgentIsolation::CurrentEnvironment.current
      return yield unless route

      agent_id = route.fetch("agent_id")
      job[ROUTE_KEY] = {
        "agent_id" => agent_id,
        "branch_id" => route.fetch("branch_id"),
        "generation" => route.fetch("generation"),
        "scheduled" => job.key?("at")
      }
      # Retried jobs can outlive an episode. Routed inference jobs fail once
      # and surface through the drain barrier instead of retrying later.
      job["retry"] = false
      @registry.enqueue_job(agent_id) unless job.key?("at")
      begin
        yield
      rescue StandardError
        @registry.complete_job(agent_id) unless job.key?("at")
        raise
      end
    end
  end

  class SidekiqServerRouteMiddleware
    def initialize(registry, connections)
      @registry = registry
      @connections = connections
    end

    def call(_worker, job, _queue)
      claims = job[SidekiqClientRouteMiddleware::ROUTE_KEY]
      return yield unless claims

      agent_id = claims.fetch("agent_id")
      route = @registry.route_for(agent_id)
      unless route.fetch("branch_id") == claims.fetch("branch_id") &&
          route.fetch("generation").to_i == claims.fetch("generation").to_i
        raise "stale WebAgent Sidekiq job for #{agent_id}"
      end

      WebAgentIsolation::CurrentEnvironment.with(route) do
        @connections.with_route(route) { yield }
      end
    ensure
      if defined?(claims) && claims && defined?(agent_id) && !claims["scheduled"]
        @registry.complete_job(agent_id)
      end
    end
  end

  module NonDBAdapter
    module_function

    def install!(registry:, connections:, search_policy: "require_disabled", cache_policy: "namespace")
      verify_search_policy!(search_policy)
      install_file_and_gitaly_routing!
      install_redis_routing!(cache_policy)
      install_sidekiq_routing!(registry: registry, connections: connections)
    end

    def verify_search_policy!(policy)
      unless policy == "require_disabled"
        raise ArgumentError, "unsupported WebAgent search policy: #{policy}"
      end

      settings = ApplicationSetting.current
      enabled = settings.elasticsearch_search || settings.elasticsearch_indexing
      return unless enabled

      raise "OpenSearch/Elasticsearch must be disabled for shared WebAgent GitLab"
    end

    def install_file_and_gitaly_routing!
      GitlabUploader.singleton_class.prepend(RoutedUploaderRoot)
      # These 15.7.5 uploaders either override `.root` or represent a required
      # correctness boundary. Referencing them first defeats Zeitwerk's lazy
      # load ordering during initializers.
      required_uploaders = [
        FileUploader,
        PersonalFileUploader,
        NamespaceFileUploader,
        JobArtifactUploader
      ]
      (required_uploaders + GitlabUploader.descendants).uniq.each do |uploader|
        # A subclass-owned `.root` sits above a module inherited from
        # GitlabUploader. Use a distinct wrapper so Ruby actually prepends it
        # to that subclass instead of treating the inherited module as enough.
        next if uploader.method(:root).owner == RoutedUploaderRoot

        wrapper = Module.new do
          define_method(:root) do
            WebAgentIsolation::RoutedUploaderRoot.route(super())
          end
        end
        uploader.singleton_class.prepend(wrapper)
      end
      raise "FileUploader root routing was not installed" if FileUploader.method(:root).owner == FileUploader.singleton_class

      GitlabUploader.prepend(RoutedUploaderInstance)
      Gitlab::GitalyClient::Util.singleton_class.prepend(RoutedGitalyRepository)
      Gitlab::Git::HookEnv.singleton_class.prepend(RoutedHookEnv)
    end

    def install_redis_routing!(cache_policy)
      Gitlab::Redis::Cache.singleton_class.prepend(RoutedCacheRedis)
      Gitlab::Redis::SharedState.singleton_class.prepend(RoutedSharedStateRedis)
      Gitlab::Redis::Sessions.singleton_class.prepend(RoutedSessionsRedis)
      Gitlab::Redis::TraceChunks.singleton_class.prepend(RoutedTraceChunksRedis)
      Gitlab::Redis::RateLimiting.singleton_class.prepend(RoutedRateLimitingRedis)

      session_namespace = lambda do
        RoutedRedis.state_namespace(Gitlab::Redis::Sessions::SESSION_NAMESPACE)
      end
      session_store = Gitlab::Redis::Sessions.store(namespace: session_namespace)
      Gitlab::Application.config.session_store(
        :redis_store,
        redis_store: session_store,
        key: "_gitlab_session",
        secure: Gitlab.config.gitlab.https,
        httponly: true,
        expires_in: Settings.gitlab["session_expire_delay"] * 60,
        path: Rails.application.config.relative_url_root.presence || "/"
      )

      case cache_policy
      when "namespace"
        cache_config = Gitlab::Redis::Cache.active_support_config.merge(
          namespace: -> { RoutedRedis.cache_namespace }
        )
        Rails.application.config.cache_store = :redis_cache_store, cache_config
        Rails.cache = ActiveSupport::Cache.lookup_store(:redis_cache_store, cache_config)
      when "disabled"
        Rails.application.config.cache_store = :null_store
        Rails.cache = ActiveSupport::Cache.lookup_store(:null_store)
      else
        raise ArgumentError, "unsupported WebAgent cache policy: #{cache_policy}"
      end
    end

    def install_sidekiq_routing!(registry:, connections:)
      Sidekiq.configure_client do |config|
        config.client_middleware do |chain|
          chain.add SidekiqClientRouteMiddleware, registry
        end
      end
      Sidekiq.configure_server do |config|
        config.client_middleware do |chain|
          chain.add SidekiqClientRouteMiddleware, registry
        end
        config.server_middleware do |chain|
          chain.add SidekiqServerRouteMiddleware, registry, connections
        end
      end
    end
  end
end
