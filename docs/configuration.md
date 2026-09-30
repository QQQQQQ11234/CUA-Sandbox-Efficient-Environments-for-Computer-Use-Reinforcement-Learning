# Configuration

Hydra loads `config.yaml`, which includes `rl_web_agent/conf/base.yaml` and the
optional `local_config.yaml`. Keep machine-specific changes in an ignored
`local_config.yaml` or environment variables; do not edit the portable base
for a single host.

## Required values for DB mode

```bash
export ROUTE_TOKEN_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export WEB_AGENT_ISOLATION_MODE=db
export DB_ADMIN_DSN='postgresql://postgres:postgres@127.0.0.1:5432/postgres'
export DB_BASE_TEMPLATE=webarena_template
export SHOPPING_SHARED_TARGET=127.0.0.1:7772
export SHOPPING_ADMIN_SHARED_TARGET=127.0.0.1:7772
```

GitLab's shared image can instead use the registry sidecar. Set
`WEB_AGENT_ROUTE_REGISTRY_SECRET` to the same secret configured in the image
and use a registry path outside source control.

## Site authorities

The browser task files and `environment.sites` must use the same logical
origins. The base configuration uses local placeholders:

```text
shopping.local:7770
shopping-admin.local:7780
reddit.local:9999
gitlab.local:8023
map.local:3000
wikipedia.local:8888
```

Replace them with your deployment's hosts using `SHOPPING_SITE_AUTHORITY`,
`GITLAB_SITE_AUTHORITY`, and the corresponding variables in `.env`.

## Credentials and model providers

Credentials are read from environment variables such as
`SHOPPING_USERNAME`, `GITLAB_PASSWORD`, and `OPENAI_API_KEY`. The base file
contains empty defaults. Choose the provider with `LLM_PROVIDER=openai` or
`LLM_PROVIDER=bedrock`; set `OPENAI_BASE_URL`, `OPENAI_MODEL`, or
`BEDROCK_MODEL_ID` as appropriate.

For evaluator calls, configure `EVALUATOR_LLM_BASE_URL`,
`EVALUATOR_LLM_API_KEY`, and `EVALUATOR_LLM_MODEL`. A local OpenAI-compatible
endpoint is a useful way to run unit and smoke tests without cloud access.

## Hydra overrides

Any setting can be overridden at the end of a command:

```bash
python -m rl_web_agent.entrypoints.batch_agent \
  --tasks_dir /data/tasks --task_ids 418 \
  environment.isolation.mode=db \
  environment.browser.launch_options.headless=true \
  agent.max_steps=30
```
