<?php
declare(strict_types=1);

require_once '/opt/web-agent-magento/web_agent_magento_route.php';
try {
    $target = WebAgentMagentoRoute::mediaPath(
        (string) ($_SERVER['WEB_AGENT_MEDIA_PATH'] ?? $_SERVER['REQUEST_URI'] ?? '')
    );
    $mime = (new finfo(FILEINFO_MIME_TYPE))->file($target) ?: 'application/octet-stream';
    header('Content-Type: ' . $mime);
    header('Content-Length: ' . (string) filesize($target));
    header('Cache-Control: private, max-age=3600');
    readfile($target);
} catch (Throwable $error) {
    http_response_code(404);
    header('Content-Type: text/plain');
    exit('not found');
}
