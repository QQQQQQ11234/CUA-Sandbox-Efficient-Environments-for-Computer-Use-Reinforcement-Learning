<?php
declare(strict_types=1);

require_once '/opt/web-agent-magento/web_agent_magento_route.php';
$config = require __DIR__ . '/env.web_agent_base.php';
return WebAgentMagentoRoute::applyDeploymentConfig($config);
