# frozen_string_literal: true

require "monitor"
require "digest"
require "set"

module WebAgentIsolation
  module GitLab157CurrentShardPool
    def pool
      ActiveRecord::Base.connection_handler.retrieve_connection_pool(
        @configuration.connection_specification_name,
        role: ActiveRecord::Base.current_role,
        shard: ActiveRecord::Base.current_shard
      ) || raise(ActiveRecord::ConnectionNotEstablished)
    end
  end

  class ConnectionManager
    # GitLab/Rails version adapter. The generic system deliberately refuses to
    # switch ActiveRecord::Base globally because that cross-routes concurrent
    # agents. Install a version-specific adapter with register_pool and
    # connected_to_route implementations before enabling shared GitLab.
    def initialize(
      adapter:,
      registry: nil,
      reap_interval_seconds: 5,
      idle_timeout_seconds: 300,
      max_registered_pools: 64,
      reaper_enabled: true
    )
      @adapter = adapter
      @registry = registry
      @reap_interval_seconds = reap_interval_seconds.to_f
      @idle_timeout_seconds = idle_timeout_seconds.to_f
      @max_registered_pools = max_registered_pools.to_i
      @reaper_enabled = reaper_enabled
      @registered = {}
      @lock = Monitor.new
      @reaper_pid = nil
      @reaper_thread = nil
    end

    def with_route(route)
      pool_key = route.fetch("pool_key")
      ensure_reaper_started
      enter(route)
      release_stale(route)
      @adapter.connected_to_route(pool_key) { yield }
    ensure
      leave(pool_key) if defined?(pool_key) && pool_key
    end

    def release(pool_key)
      @lock.synchronize do
        metadata = @registered[pool_key]
        return false unless metadata
        return false if metadata[:active_requests].positive?

        @adapter.release_pool(pool_key)
        @registered.delete(pool_key)
        true
      end
    end

    def registered_pool_count
      @lock.synchronize { @registered.length }
    end

    def reap_once
      return [] unless @registry

      active_pool_keys = @registry.active_pool_keys.to_set
      now = monotonic_time
      candidates = @lock.synchronize do
        orphaned = @registered.filter_map do |pool_key, metadata|
          next if metadata[:active_requests].positive?
          next if active_pool_keys.include?(pool_key) &&
            now - metadata[:last_used_at] < @idle_timeout_seconds

          pool_key
        end

        remaining_idle = @registered.filter_map do |pool_key, metadata|
          next if orphaned.include?(pool_key)
          next if metadata[:active_requests].positive?

          [pool_key, metadata[:last_used_at]]
        end
        overflow = [@registered.length - orphaned.length - @max_registered_pools, 0].max
        orphaned + remaining_idle.sort_by(&:last).first(overflow).map(&:first)
      end
      candidates.uniq.select { |pool_key| release(pool_key) }
    end

    private

    def enter(route)
      pool_key = route.fetch("pool_key")
      @lock.synchronize do
        unless @registered[pool_key]
          @adapter.register_pool(pool_key, route.fetch("db_name"))
          @registered[pool_key] = {
            environment_id: route.fetch("environment_id"),
            branch_id: route.fetch("branch_id"),
            generation: route.fetch("generation").to_i,
            active_requests: 0,
            last_used_at: monotonic_time
          }
        end
        @registered.fetch(pool_key)[:active_requests] += 1
      end
    end

    def leave(pool_key)
      @lock.synchronize do
        metadata = @registered[pool_key]
        return unless metadata

        metadata[:active_requests] -= 1
        raise "WebAgent pool request counter underflow: #{pool_key}" if metadata[:active_requests].negative?

        metadata[:last_used_at] = monotonic_time
      end
    end

    def release_stale(route)
      stale = @lock.synchronize do
        @registered.filter_map do |pool_key, metadata|
          next if pool_key == route.fetch("pool_key")
          next unless metadata[:environment_id] == route.fetch("environment_id")
          next unless metadata[:branch_id] == route.fetch("branch_id")
          next unless metadata[:generation] < route.fetch("generation").to_i

          pool_key
        end
      end
      stale.each { |pool_key| release(pool_key) }
    end

    def ensure_reaper_started
      return unless @reaper_enabled && @registry

      pid = Process.pid
      @lock.synchronize do
        return if @reaper_pid == pid && @reaper_thread&.alive?

        @reaper_pid = pid
        @reaper_thread = Thread.new do
          Thread.current.name = "web-agent-pool-reaper" if Thread.current.respond_to?(:name=)
          Thread.current.report_on_exception = false
          loop do
            sleep @reap_interval_seconds
            begin
              reap_once
            rescue StandardError => error
              warn("WebAgent pool reaper failed: #{error.class}: #{error.message}")
            end
          end
        end
      end
    end

    def monotonic_time
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end
  end

  class UnsupportedActiveRecordAdapter
    def register_pool(pool_key, database_name)
      raise NotImplementedError, "implement GitLab-version-specific ActiveRecord pool registration for #{pool_key} -> #{database_name}"
    end

    def connected_to_route(pool_key)
      raise NotImplementedError, "implement request-scoped ActiveRecord pool selection for #{pool_key}"
    end

    def release_pool(_pool_key); end
  end

  class GitLab157ActiveRecordAdapter
    ROLE = :writing

    def initialize(base_class: ActiveRecord::Base, connection_overrides: {})
      @base_class = base_class
      @connection_overrides = connection_overrides.transform_keys(&:to_sym)
      @shards = {}
      @lock = Monitor.new
      install_current_shard_pool!
    end

    def register_pool(pool_key, database_name)
      shard = shard_for(pool_key)
      config = @base_class.connection_db_config.configuration_hash
        .merge(@connection_overrides)
        .merge(database: database_name)
      connection_handler.establish_connection(
        config,
        owner_name: @base_class,
        role: ROLE,
        shard: shard
      )
    end

    def connected_to_route(pool_key, &block)
      @base_class.connected_to(role: ROLE, shard: shard_for(pool_key), &block)
    end

    def release_pool(pool_key)
      shard = @lock.synchronize { @shards[pool_key] }
      return unless shard

      connection_handler.remove_connection_pool(@base_class, role: ROLE, shard: shard)
      @lock.synchronize { @shards.delete(pool_key) if @shards[pool_key] == shard }
    end

    private

    def shard_for(pool_key)
      @lock.synchronize do
        @shards[pool_key] ||= "web_agent_#{Digest::SHA256.hexdigest(pool_key)[0, 20]}".to_sym
      end
    end

    def connection_handler
      @base_class.connection_handler
    end

    def install_current_shard_pool!
      load_balancer = Gitlab::Database::LoadBalancing::LoadBalancer
      return if load_balancer < GitLab157CurrentShardPool

      load_balancer.prepend(GitLab157CurrentShardPool)
    end
  end
end
