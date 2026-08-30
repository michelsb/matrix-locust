# Build layer to clone the repository and install dependencies
FROM python:3.11-alpine as builder

# Install git, gcc, and other build tools
RUN apk add --no-cache curl git gcc libffi-dev musl-dev openssl-dev poetry python3-dev

# Set the working directory
WORKDIR /usr/src/app

# Clone the repository into the working directory
RUN git clone https://gitlab.futo.org/load-testing/matrix-locust.git .

# Fix externally-managed-envrionment errors: https://stackoverflow.com/a/76641565
RUN rm /usr/lib/python3.11/EXTERNALLY-MANAGED

# Install the dependencies using Poetry without creating a virtual environment
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-root

# Main image
FROM python:3.11-alpine

# Set the working directory
WORKDIR /usr/src/app

# Copy the cloned repository from the builder layer
COPY --from=builder /usr/src/app .

# Copy the installed Python packages from the builder layer
COPY --from=builder /usr/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/bin /usr/bin

# Update the PATH environment variable to include the cloned repository directory
ENV PATH="/usr/src/app:${PATH}"

# Expose web UI running from container
EXPOSE 8089

# No entrypoint, this line is kept to keep the container running to launch commands
CMD ["tail", "-f", "/dev/null"]
