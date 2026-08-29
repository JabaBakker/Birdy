FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/
COPY prompts/ prompts/

ENV AGENT_WORKSPACE=/data/workspace
ENV TZ=Europe/Amsterdam
RUN git config --global user.name "Fien" \
    && git config --global user.email "fien@localhost" \
    && git config --global init.defaultBranch main

CMD ["python", "-m", "agent.main"]
