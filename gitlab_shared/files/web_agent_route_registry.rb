# frozen_string_literal: true

require "json"
require "net/http"
require "uri"

module WebAgentIsolation
  class RouteFrozen < StandardError; end

  class RouteRegistry
    def initialize(url:, secret:, read_timeout_seconds: 10)
      @url = url.sub(%r{/$}, "")
      @secret = secret
      @read_timeout_seconds = read_timeout_seconds.to_f
    end

    def database_for(agent_id)
      route_for(agent_id).fetch("db_name")
    end

    def route_for(agent_id)
      request(agent_id, Net::HTTP::Get)
    end

    def active_pool_keys
      request_collection.fetch("routes").map { |route| route.fetch("pool_key") }
    end

    def acquire(agent_id)
      request(agent_id, Net::HTTP::Post, "acquire")
    end

    def release(agent_id)
      request(agent_id, Net::HTTP::Post, "release")
    end

    def enqueue_job(agent_id)
      request(agent_id, Net::HTTP::Post, "enqueue-job")
    end

    def complete_job(agent_id)
      request(agent_id, Net::HTTP::Post, "complete-job")
    end

    private

    def request_collection
      uri = URI("#{@url}/v1/routes")
      request = Net::HTTP::Get.new(uri)
      request["Authorization"] = "Bearer #{@secret}"
      response = perform(uri, request)
      raise "route registry returned HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)

      JSON.parse(response.body)
    end

    def request(agent_id, request_class, action = nil)
      path = "#{@url}/v1/routes/#{URI.encode_www_form_component(agent_id)}"
      path += "/#{action}" if action
      uri = URI(path)
      request = request_class.new(uri)
      request["Authorization"] = "Bearer #{@secret}"
      response = perform(uri, request)
      raise KeyError, "unknown WebAgent route" if response.code.to_i == 404
      raise RouteFrozen, "WebAgent route is frozen" if response.code.to_i == 423
      raise "route registry returned HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)

      JSON.parse(response.body)
    end

    def perform(uri, request)
      Net::HTTP.start(
        uri.host,
        uri.port,
        use_ssl: uri.scheme == "https",
        open_timeout: 2,
        read_timeout: @read_timeout_seconds
      ) { |http| http.request(request) }
    end
  end
end
