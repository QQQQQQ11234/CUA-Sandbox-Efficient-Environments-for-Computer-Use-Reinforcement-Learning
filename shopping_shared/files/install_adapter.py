from pathlib import Path


path = Path("/var/www/magento2/nginx.conf.sample")
lines = path.read_text().splitlines()
start = next(i for i, line in enumerate(lines) if line.strip() == "location /media/ {")
end = next(
    i for i, line in enumerate(lines[start + 1 :], start + 1)
    if line.strip() == "location /media/customer/ {"
)
replacement = [
    "location /media/ {",
    "    include fastcgi_params;",
    "    fastcgi_pass fastcgi_backend;",
    "    fastcgi_param SCRIPT_FILENAME /opt/web-agent-magento/web_agent_media.php;",
    "    fastcgi_param WEB_AGENT_MEDIA_PATH $uri;",
    "}",
    "",
]
path.write_text("\n".join([*lines[:start], *replacement, *lines[end:]]) + "\n")
