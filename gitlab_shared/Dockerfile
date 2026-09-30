ARG WEB_ARENA_GITLAB_IMAGE
FROM ${WEB_ARENA_GITLAB_IMAGE}

USER root

COPY files/web_agent_database_router.rb /opt/web-agent/web_agent_database_router.rb
COPY files/web_agent_route_token.rb /opt/web-agent/web_agent_route_token.rb
COPY files/web_agent_route_registry.rb /opt/web-agent/web_agent_route_registry.rb
COPY files/web_agent_connection_manager.rb /opt/web-agent/web_agent_connection_manager.rb
COPY files/web_agent_initializer.rb /opt/web-agent/web_agent_initializer.rb
COPY files/gitlab_statectl.rb /opt/web-agent/gitlab_statectl.rb
COPY files/web_agent_non_db_adapter.rb /opt/web-agent/web_agent_non_db_adapter.rb
COPY files/configure_local_url.sh /opt/web-agent/configure_local_url.sh
COPY files/web_agent_initializer.rb /opt/gitlab/embedded/service/gitlab-rails/config/initializers/web_agent_initializer.rb

RUN chmod 0644 /opt/web-agent/*.rb && chmod 0755 /opt/web-agent/configure_local_url.sh

# The exact initializer path and Rails boot hook depend on the WebArena GitLab
# version. build_shared_gitlab.sh inspects the source tree and installs these
# files only after the expected GitLab Rails directories are found.
