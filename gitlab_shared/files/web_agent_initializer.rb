# frozen_string_literal: true

require "digest"
require "json"
require "/opt/web-agent/web_agent_database_router"
require "/opt/web-agent/web_agent_non_db_adapter"

config_path = ENV.fetch("WEB_AGENT_CONFIG", "/etc/web-agent/config.json")
config = JSON.parse(File.read(config_path))
route_secret = config.fetch("route_secret")
registry_url = config.fetch("route_registry_url")
registry_secret = config.fetch("route_registry_secret")
database_connection = config.fetch("database_connection", {}).dup
if database_connection.delete("password_from_route_secret")
  database_connection["password"] = route_secret
end

token_verifier = WebAgentIsolation::RouteToken.new(secret: route_secret)
registry = WebAgentIsolation::RouteRegistry.new(
  url: registry_url,
  secret: registry_secret,
  read_timeout_seconds: config.fetch("route_registry_read_timeout_seconds", 10)
)
adapter = WebAgentIsolation::GitLab157ActiveRecordAdapter.new(
  connection_overrides: database_connection
)
connections = WebAgentIsolation::ConnectionManager.new(
  adapter: adapter,
  registry: registry,
  reap_interval_seconds: config.fetch("pool_reap_interval_seconds", 5),
  idle_timeout_seconds: config.fetch("pool_idle_timeout_seconds", 300),
  max_registered_pools: config.fetch("max_registered_pools", 64)
)
probe_enabled = config.fetch("probe_enabled", false)
non_db_state_enabled = config.fetch("non_db_state_enabled", false)
search_policy = config.fetch("search_policy", "require_disabled")
cache_policy = config.fetch("cache_policy", "namespace")

if non_db_state_enabled
  WebAgentIsolation::NonDBAdapter.install!(
    registry: registry,
    connections: connections,
    search_policy: search_policy,
    cache_policy: cache_policy
  )
end

Rails.application.config.middleware.insert_before(
  0,
  WebAgentIsolation::DatabaseRouter,
  token_verifier: token_verifier,
  registry: registry,
  connections: connections,
  probe_enabled: probe_enabled
)
