# HavenIQ voice support agent — LiveKit Agents worker. Runs in the haveniq namespace, dials OUT to the
# LiveKit media plane, talks to the platform's model desk for its turns and to the MCP gateway for every
# tool call. Same base and build shape as upstreambeat's live worker.
FROM registry.access.redhat.com/ubi9/python-312:latest
USER 0
WORKDIR /app
COPY voice/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY voice/agent.py .
RUN python agent.py download-files && chgrp -R 0 /app /opt/app-root && chmod -R g=u /app /opt/app-root
USER 1001
ENV HOME=/app
CMD ["python", "agent.py", "start"]
