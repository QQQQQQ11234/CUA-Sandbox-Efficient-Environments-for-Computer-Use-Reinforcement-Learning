# frozen_string_literal: true

require "fileutils"
require "json"
require "open3"
require "redis"

module WebAgentStatectl
  STATE_ROOT = "/var/opt/gitlab/web-agent-state"
  GITALY_ROOT = "/var/opt/gitlab/git-data/repositories"
  SOURCES = {
    "uploads" => "/var/opt/gitlab/gitlab-rails/uploads",
    "artifacts" => "/var/opt/gitlab/gitlab-rails/shared/artifacts",
    "gitaly" => GITALY_ROOT
  }.freeze

  module_function

  def token(value)
    normalized = value.to_s.gsub(/[^A-Za-z0-9_.-]/, "_").gsub(/_+/, "_").sub(/\A[_.]+/, "").sub(/[_.]+\z/, "")
    raise ArgumentError, "empty state identity" if normalized.empty?

    normalized
  end

  def base(component)
    if component == "gitaly"
      File.join(GITALY_ROOT, ".web-agent", "base", "gitaly")
    else
      File.join(STATE_ROOT, "base", component)
    end
  end

  def branch(environment, branch_id, component)
    if component == "gitaly"
      File.join(GITALY_ROOT, ".web-agent", "environments", environment, "branches", branch_id, "gitaly")
    else
      File.join(STATE_ROOT, "environments", environment, "branches", branch_id, component)
    end
  end

  def checkpoint(environment, checkpoint_id, component)
    if component == "gitaly"
      File.join(GITALY_ROOT, ".web-agent", "environments", environment, "checkpoints", checkpoint_id, component)
    else
      File.join(STATE_ROOT, "environments", environment, "checkpoints", checkpoint_id, component)
    end
  end

  def clone_tree(source, target, require_reflink: true, exclude: nil)
    raise "missing state source: #{source}" unless File.directory?(source)

    FileUtils.rm_rf(target)
    FileUtils.mkdir_p(target)
    reflink = require_reflink ? "--reflink=always" : "--reflink=auto"
    entries =
      if exclude
        Dir.children(source).reject { |entry| entry == exclude }.map { |entry| File.join(source, entry) }
      else
        ["#{source}/."]
      end
    entries.each do |entry|
      stdout, stderr, status = Open3.capture3("cp", "-a", reflink, entry, target)
      message = stderr.empty? ? stdout : stderr
      raise "state clone failed: #{message}" unless status.success?
    end
  end

  def ensure_base!
    SOURCES.each do |component, source|
      target = base(component)
      next if File.file?(File.join(target, ".web-agent-base-ready"))

      clone_tree(
        source,
        target,
        require_reflink: false,
        exclude: (".web-agent" if component == "gitaly")
      )
      File.write(File.join(target, ".web-agent-base-ready"), "GitLab 15.7.5\n")
    end
  end

  def prepare(environment, branch_id)
    ensure_base!
    SOURCES.each_key do |component|
      target = branch(environment, branch_id, component)
      clone_tree(base(component), target) unless File.directory?(target)
    end
  end

  def redis
    @redis ||= Redis.new(path: "/var/opt/gitlab/redis/redis.socket")
  end

  def state_prefix(environment, site, branch_id)
    "state:{webagent:#{environment}:#{site}:#{branch_id}}:"
  end

  def each_key(pattern)
    redis.scan_each(match: pattern, count: 1_000) { |key| yield key }
  end

  def delete_keys(pattern)
    batch = []
    each_key(pattern) do |key|
      batch << key
      if batch.length >= 1_000
        redis.unlink(*batch)
        batch.clear
      end
    end
    redis.unlink(*batch) unless batch.empty?
  end

  def redis_checkpoint_path(environment, checkpoint_id)
    File.join(STATE_ROOT, "environments", environment, "checkpoints", checkpoint_id, "redis.dump")
  end

  def checkpoint_authoritative_redis(environment, site, branch_id, checkpoint_id)
    source_prefix = state_prefix(environment, site, branch_id)
    entries = []
    each_key("#{source_prefix}*") do |source_key|
      payload = redis.dump(source_key)
      next unless payload

      ttl = redis.pttl(source_key)
      ttl = 0 if ttl.negative?
      entries << [source_key.delete_prefix(source_prefix), ttl, payload]
    end
    target = redis_checkpoint_path(environment, checkpoint_id)
    FileUtils.mkdir_p(File.dirname(target))
    temporary = "#{target}.tmp-#{Process.pid}"
    File.binwrite(temporary, Marshal.dump(entries))
    File.rename(temporary, target)
  ensure
    FileUtils.rm_f(temporary) if defined?(temporary) && temporary
  end

  def restore_authoritative_redis(environment, site, checkpoint_id, target_branch)
    target_prefix = state_prefix(environment, site, target_branch)
    delete_keys("#{target_prefix}*")
    source = redis_checkpoint_path(environment, checkpoint_id)
    raise "missing Redis checkpoint: #{source}" unless File.file?(source)

    Marshal.load(File.binread(source)).each do |suffix, ttl, payload|
      redis.restore("#{target_prefix}#{suffix}", ttl, payload, replace: true)
    end
  end

  def clear_environment_redis(environment, site)
    delete_keys("state:{webagent:#{environment}:#{site}:*}:*")
    delete_keys("cache:{webagent:#{environment}:#{site}:*}:*")
  end

  def clear_scheduled_jobs(environment)
    %w[schedule retry dead].each do |key|
      redis.zscan_each(key) do |payload, _score|
        job = JSON.parse(payload)
        route = job["web_agent_route"]
        redis.zrem(key, payload) if route && route["agent_id"] == environment
      rescue JSON::ParserError
        next
      end
    end
  end

  def create_checkpoint(environment, site, branch_id, checkpoint_id)
    SOURCES.each_key do |component|
      clone_tree(branch(environment, branch_id, component), checkpoint(environment, checkpoint_id, component))
    end
    checkpoint_authoritative_redis(environment, site, branch_id, checkpoint_id)
  end

  def fork(environment, site, _source_branch, checkpoint_id, branch_id)
    SOURCES.each_key do |component|
      clone_tree(checkpoint(environment, checkpoint_id, component), branch(environment, branch_id, component))
    end
    restore_authoritative_redis(environment, site, checkpoint_id, branch_id)
  end

  def reset(environment, site, branch_id)
    cleanup(environment, site)
    prepare(environment, branch_id)
  end

  def cleanup(environment, site)
    FileUtils.rm_rf(File.join(STATE_ROOT, "environments", environment))
    FileUtils.rm_rf(File.join(GITALY_ROOT, ".web-agent", "environments", environment))
    clear_environment_redis(environment, site)
    clear_scheduled_jobs(environment)
  end

  def main(argv)
    operation = argv.shift || raise(ArgumentError, "missing operation")
    case operation
    when "prepare"
      prepare(token(argv.fetch(0)), token(argv.fetch(2)))
    when "checkpoint"
      create_checkpoint(
        token(argv.fetch(0)),
        token(argv.fetch(1)),
        token(argv.fetch(2)),
        token(argv.fetch(3))
      )
    when "fork"
      fork(
        token(argv.fetch(0)),
        token(argv.fetch(1)),
        token(argv.fetch(2)),
        token(argv.fetch(3)),
        token(argv.fetch(4))
      )
    when "reset"
      reset(token(argv.fetch(0)), token(argv.fetch(1)), token(argv.fetch(2)))
    when "cleanup"
      cleanup(token(argv.fetch(0)), token(argv.fetch(1)))
    else
      raise ArgumentError, "unsupported operation: #{operation}"
    end
  end
end

WebAgentStatectl.main(ARGV)
