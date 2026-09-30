# frozen_string_literal: true

require "minitest/autorun"
require_relative "../files/web_agent_connection_manager"

class FakePoolAdapter
  attr_reader :registered, :released

  def initialize
    @registered = []
    @released = []
  end

  def register_pool(pool_key, database_name)
    @registered << [pool_key, database_name]
  end

  def connected_to_route(_pool_key)
    yield
  end

  def release_pool(pool_key)
    @released << pool_key
  end
end

class FailOncePoolAdapter < FakePoolAdapter
  def initialize
    super
    @fail_once = true
  end

  def release_pool(pool_key)
    if @fail_once
      @fail_once = false
      raise "release failed"
    end

    super
  end
end

class FakeRouteRegistry
  attr_accessor :active_pool_keys

  def initialize(active_pool_keys)
    @active_pool_keys = active_pool_keys
  end
end

class ConnectionManagerTest < Minitest::Test
  def route(pool_key, generation: 1)
    {
      "pool_key" => pool_key,
      "db_name" => "agent_db",
      "environment_id" => "environment_a",
      "branch_id" => "root",
      "generation" => generation
    }
  end

  def build_manager(adapter, registry, **options)
    WebAgentIsolation::ConnectionManager.new(
      adapter: adapter,
      registry: registry,
      reaper_enabled: false,
      idle_timeout_seconds: 3600,
      **options
    )
  end

  def test_orphaned_pool_is_reaped
    adapter = FakePoolAdapter.new
    registry = FakeRouteRegistry.new(["pool-a"])
    manager = build_manager(adapter, registry)
    manager.with_route(route("pool-a")) { nil }

    registry.active_pool_keys = []
    assert_equal ["pool-a"], manager.reap_once
    assert_equal ["pool-a"], adapter.released
    assert_equal 0, manager.registered_pool_count
  end

  def test_reaper_never_releases_pool_with_active_request
    adapter = FakePoolAdapter.new
    registry = FakeRouteRegistry.new([])
    manager = build_manager(adapter, registry)

    manager.with_route(route("pool-a")) do
      assert_empty manager.reap_once
      assert_empty adapter.released
    end
    assert_equal ["pool-a"], manager.reap_once
  end

  def test_pool_limit_releases_oldest_idle_pool
    adapter = FakePoolAdapter.new
    registry = FakeRouteRegistry.new(%w[pool-a pool-b])
    manager = build_manager(adapter, registry, max_registered_pools: 1)
    manager.with_route(route("pool-a")) { nil }
    manager.with_route(route("pool-b")) { nil }

    assert_equal ["pool-a"], manager.reap_once
    assert_equal 1, manager.registered_pool_count
  end

  def test_failed_release_remains_registered_for_retry
    adapter = FailOncePoolAdapter.new
    registry = FakeRouteRegistry.new([])
    manager = build_manager(adapter, registry)
    manager.with_route(route("pool-a")) { nil }

    assert_raises(RuntimeError) { manager.reap_once }
    assert_equal 1, manager.registered_pool_count
    assert_equal ["pool-a"], manager.reap_once
    assert_equal 0, manager.registered_pool_count
  end
end
