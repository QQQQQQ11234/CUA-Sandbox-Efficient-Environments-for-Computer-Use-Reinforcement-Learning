# Shared Shopping runtime

This image keeps one Magento/Nginx/PHP-FPM/Redis/OpenSearch stack and disables
the image's embedded MySQL and cron processes. Every PHP request must carry a
signed `X-Agent-Route` capability. The adapter acquires the route drain lease
and routes MySQL, Redis cache keys, file sessions, media/var paths, and the
Magento search-index prefix from the trusted registry record.
Nginx also sends `/media/` through a small PHP file server. It checks the
branch delta first and then the shared image's immutable storefront baseline.
Media replacement/deletion is deliberately fail-closed by the task contract.

Build the storefront and Admin runtimes separately. Their Magento code is not
interchangeable, so the Dockerfile deliberately has no usable default base:

```bash
docker build --build-arg BASE_IMAGE=webarenaimages/shopping_final_0712:latest \
  -t webarena-shopping-shared:dev shopping_shared
docker build --build-arg BASE_IMAGE=webarenaimages/shopping_admin_final_0719:latest \
  -t webarena-shopping-admin-shared:dev shopping_shared
```

Build both baselines with `setup_mysql_template.sh` (the default `shopping`
source) and
`MAGENTO_SITE=shopping_admin MAGENTO_SOURCE_CONTAINER=shopping_admin setup_mysql_template.sh`.
The two official images have materially different databases and must never
share one template. Provisioning then continues with
`MAGENTO_SITE=shopping_admin setup_mysql_runtime_container.sh`,
`setup_shopping_route_registry.sh`, then
`setup_shopping_shared.sh`. Set `MAGENTO_SHARED_SITE=shopping_admin` when
starting the CMS runtime; the setup script selects the official Admin base
image, `shared-shopping-admin`, and the official port 7780. A Docker baseline
used for comparison must run on a separate port (for example 7781); browser URL
rewrites are deliberately avoided because redirect requests can escape them.
The DB manager and registry share
`runtime/shopping_db_routes.sqlite3`.

The container requires these environment variables:

- `WEB_AGENT_ROUTE_TOKEN_SECRET`
- `WEB_AGENT_ROUTE_REGISTRY_SECRET`
- `WEB_AGENT_ROUTE_REGISTRY_URL` (reachable from the container)
- `WEB_AGENT_MAGENTO_STATE_ROOT=/var/www/magento2/.web-agent-state` (the
  branch mount must remain below Magento's base directory because Magento
  validates theme/media paths against that root)
- `WEB_AGENT_MAGENTO_CRYPT_KEY_SHOPPING` and
  `WEB_AGENT_MAGENTO_CRYPT_KEY_SHOPPING_ADMIN` (trusted deployment values;
  `setup_shopping_shared.sh` reads them from the two official images)

Mount the host Magento state runtime at
`/var/www/magento2/.web-agent-state`.
The route registry and MySQL advertised host must also be reachable from the
container. Requests without a valid route fail closed with HTTP 403; frozen
routes return HTTP 503.
