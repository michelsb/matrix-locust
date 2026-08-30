#!/bin/env python3

import csv
import logging
import resource

from locust import task, constant
from locust import events
from locust.runners import MasterRunner, WorkerRunner

import gevent
from matrix_locust.users.matrixuser import MatrixUser
from matrix_locust.data import USERS_CSV
from nio.responses import RegisterErrorResponse

# Preflight ####################################################################

@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    # Increase resource limits to prevent OS running out of descriptors
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except ValueError as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    # Multi-worker
    if isinstance(environment.runner, WorkerRunner):
        print(f"Registered 'load_users' handler on {environment.runner.client_id}")
        environment.runner.register_message("load_users", MatrixRegisterUser.load_users)
    # Single-worker
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(environment.runner, MasterRunner):
        # Open our list of users
        MatrixRegisterUser.worker_users = csv.DictReader(USERS_CSV.open(encoding="utf-8"))

################################################################################


class MatrixRegisterUser(MatrixUser):
    wait_time = constant(0)
    worker_id = None
    worker_users = []

    @staticmethod
    def load_users(environment, msg, **_kwargs):
        MatrixRegisterUser.worker_users = iter(msg.data)
        MatrixRegisterUser.worker_id = environment.runner.client_id
        logging.info("Worker [%s] Received %s users", environment.runner.client_id, len(msg.data))

    @task
    def register_user(self):
        # Multiple locust users re-use the same class instance, so need to reset the state
        self.reset_client()

        # Load the next user who needs to be registered
        try:
            user = next(MatrixRegisterUser.worker_users)
        except StopIteration:
            # We can't shut down the worker until all users are registered, so return
            # early to stop this individual co-routine
            gevent.sleep(999999)
            return

        self.set_user(user["username"])
        self.matrix_client.password = user["password"]

        if self.matrix_client.user is None or self.matrix_client.password is None:
            logging.error("Couldn't get username/password. Skipping...")
            return

        retries = 3
        while retries > 0:
            # Register with the server to get a user_id and access_token
            response = self.matrix_client.register(self.matrix_client.user, self.matrix_client.password, token="")

            if isinstance(response, RegisterErrorResponse):
                logging.info("[%s] Could not register user (attempt %d). Trying again...",
                             self.matrix_client.user, 4 - retries)
                retries -= 1
                continue

            return

        logging.error("Error registering user %s. Skipping...", self.matrix_client.user)
