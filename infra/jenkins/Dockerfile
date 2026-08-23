# Jenkins LTS with a Docker CLI, and nothing else added by hand.
#
# **Docker-outside-of-Docker, not Docker-in-Docker.** The container gets the
# host's Docker socket and a client to talk to it, rather than running its own
# privileged daemon. DinD needs `--privileged`, which on this project buys
# nothing: the builds want *a* daemon, not an isolated one, and reusing the
# host's means the layer cache is shared with every other build on this machine
# instead of being rebuilt from scratch inside a nested daemon.
#
# The trade is real and worth stating: a job that can talk to the host's Docker
# socket can do anything root can do on the host. That is acceptable for a local
# demo of the pipeline and would not be acceptable on a shared controller, where
# the answer is an agent per job with its own daemon.

FROM jenkins/jenkins:lts-jdk21

USER root

# The Docker CLI (not the daemon) plus the compose plugin, from Docker's own
# repository so the client version tracks the server's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 make \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Plugins are pinned in a file rather than installed from the UI: a controller
# whose plugin set exists only in its volume is a controller nobody can rebuild.
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Skip the setup wizard: everything it would ask is answered by JCasC below, and
# a manual first-run step is the thing that makes a controller unreproducible.
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
ENV CASC_JENKINS_CONFIG=/var/jenkins_home/casc.yaml

# Jenkins writes a sibling `<job>@tmp` directory next to every workspace. When
# the workspace itself is a bind mount, Docker creates the missing parent
# directories as **root**, and the jenkins user then cannot create that sibling
# -- every `sh` step fails with AccessDeniedException before running a command.
# Creating it in the image means the named volume is seeded with the right
# ownership on first start.
RUN mkdir -p /var/jenkins_home/workspace/resumeforge \
    && chown -R jenkins:jenkins /var/jenkins_home/workspace

USER jenkins
