FROM python:3.11-slim-trixie

WORKDIR /app
COPY rl_web_agent /app/rl_web_agent
RUN pip install --no-cache-dir omegaconf

ENTRYPOINT ["python", "-m", "rl_web_agent.entrypoints.route_registry_server"]
