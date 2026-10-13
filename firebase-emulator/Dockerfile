FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y openjdk-17-jre-headless curl \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global firebase-tools@14.15.2

RUN firebase setup:emulators:firestore \
    && firebase setup:emulators:ui

WORKDIR /workspace
COPY firebase.json firestore.rules ./

EXPOSE 4000 8080

CMD ["firebase", "emulators:start", "--only", "firestore", "--project", "demo-yagapon-support", "--config", "/workspace/firebase.json"]
