# frozen_string_literal: true

require "base64"
require "json"
require "openssl"

module WebAgentIsolation
  class InvalidRouteToken < StandardError; end

  class RouteToken
    def initialize(secret:, clock: -> { Time.now.to_i })
      raise ArgumentError, "route secret must contain at least 32 bytes" if secret.bytesize < 32

      @secret = secret
      @clock = clock
    end

    def verify!(token)
      payload_part, signature_part = token.to_s.split(".", 2)
      raise InvalidRouteToken, "malformed route token" unless payload_part && signature_part

      payload = Base64.urlsafe_decode64(pad(payload_part))
      signature = Base64.urlsafe_decode64(pad(signature_part))
      expected = OpenSSL::HMAC.digest("SHA256", @secret, payload)
      raise InvalidRouteToken, "invalid route token signature" unless secure_compare(signature, expected)

      claims = JSON.parse(payload)
      raise InvalidRouteToken, "unsupported token version" unless claims.fetch("v") == 1
      raise InvalidRouteToken, "route token expired" if claims.fetch("exp").to_i < @clock.call

      claims.fetch("agent_id")
    rescue JSON::ParserError, KeyError, ArgumentError
      raise InvalidRouteToken, "invalid route token payload"
    end

    private

    def pad(value)
      value + ("=" * ((4 - value.length % 4) % 4))
    end

    def secure_compare(left, right)
      left.bytesize == right.bytesize && OpenSSL.fixed_length_secure_compare(left, right)
    end
  end
end
