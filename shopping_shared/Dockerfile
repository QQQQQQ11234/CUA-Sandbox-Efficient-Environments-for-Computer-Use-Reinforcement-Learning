# The storefront and Admin WebArena images contain different Magento code.
# Require callers to choose one explicitly so an Admin database can never be
# served accidentally by the storefront runtime (or vice versa).
ARG BASE_IMAGE=web-agent-base-image-must-be-set
FROM ${BASE_IMAGE}

ARG WEB_AGENT_STATE_GID=1021

COPY files/web_agent_magento_route.php /opt/web-agent-magento/web_agent_magento_route.php
COPY files/web_agent_bootstrap.php /opt/web-agent-magento/web_agent_bootstrap.php
COPY files/web_agent_media.php /opt/web-agent-magento/web_agent_media.php
COPY files/install_adapter.py /opt/web-agent-magento/install_adapter.py
COPY files/env.php /tmp/web-agent-env.php

RUN set -eu; \
    addgroup -g "${WEB_AGENT_STATE_GID}" web-agent-state; \
    sed -i 's/^group = www-data$/group = web-agent-state/' \
      /usr/local/etc/php-fpm.d/www.conf; \
    test -f /var/www/magento2/app/etc/env.php; \
    mv /var/www/magento2/app/etc/env.php /var/www/magento2/app/etc/env.web_agent_base.php; \
    mv /tmp/web-agent-env.php /var/www/magento2/app/etc/env.php; \
    python3 /opt/web-agent-magento/install_adapter.py; \
    printf '%s\n' 'auto_prepend_file=/opt/web-agent-magento/web_agent_bootstrap.php' \
      > /usr/local/etc/php/conf.d/zz-web-agent-route.ini; \
    sed -i 's/^autostart=true$/autostart=false/' /etc/supervisor.d/mysql.ini; \
    sed -i 's/^autostart=true$/autostart=false/' /etc/supervisor.d/cron.ini; \
    chown -R root:root /opt/web-agent-magento; \
    chmod 0755 /opt/web-agent-magento /opt/web-agent-magento/*.php
