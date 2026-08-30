#!/bin/env python3

import csv
import resource
import logging

from locust import task, constant
from locust import events
from locust.runners import MasterRunner, WorkerRunner

import gevent
from matrix_locust.users.matrixuser import MatrixUser
from matrix_locust.data import USERS_CSV
from nio.responses import JoinError, LoginError, SyncError

# Preflight ###############################################

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
        environment.runner.register_message("load_users", MatrixInviteAcceptorUser.load_users)
    # Single-worker
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(environment.runner, MasterRunner):
        # Open our list of users
        MatrixInviteAcceptorUser.worker_users = csv.DictReader(USERS_CSV.open(encoding="utf-8"))

###########################################################


class MatrixInviteAcceptorUser(MatrixUser):
    wait_time = constant(0)

    worker_id = None
    worker_users = []

    @staticmethod
    def load_users(environment, msg, **_kwargs):
        MatrixInviteAcceptorUser.worker_users = iter(msg.data)
        MatrixInviteAcceptorUser.worker_id = environment.runner.client_id
        logging.info("Worker [%s]: Received %s users", environment.runner.client_id, len(msg.data))

    @task
    def accept_invites(self):
        # Multiple locust users re-use the same class instance, so need to reset the state
        self.reset_client()

        # Load the next user
        try:
            user = next(MatrixInviteAcceptorUser.worker_users)
        except StopIteration:
            # We can't shut down the worker until all users are registered, so return
            # early to stop this individual co-routine
            gevent.sleep(999999)
            return

        self.login_from_csv(user)

        if self.matrix_client.user is None or self.matrix_client.password is None:
            logging.error("Couldn't get username/password. Skipping...")
            return

        # Log in as this current user if not already logged in
        if self.matrix_client.user_id is None or self.matrix_client.access_token is None or \
            len(self.matrix_client.user_id) < 1 or len(self.matrix_client.access_token) < 1:

            response = self.matrix_client.login(self.matrix_client.password)

            if isinstance(response, LoginError):
                logging.error("Login failed for User [%s]", self.matrix_client.user)
                return

        # Call /sync to get our list of invited rooms
        response = self.matrix_client.sync()

        if isinstance(response, SyncError):
            logging.error("[%s] /sync error (%s): %s", self.matrix_client.user,
                          response.status_code, response.message)

        invited_rooms = self.matrix_client.invited_rooms.keys()

        logging.info("User [%s] has %d pending invites", self.matrix_client.user, len(invited_rooms))
        for room_id in invited_rooms:
            retries = 3
            while retries > 0:
                response = self.matrix_client.join(room_id)

                if isinstance(response, JoinError):
                    logging.error("[%s] Could not join room %s (attempt %d). Trying again...",
                                 self.matrix_client.user, room_id, 4 - retries)
                    logging.error("[%s] Code=%s, Message=%s", self.matrix_client.user,
                                  response.status_code, response.message)
                    retries -= 1
                else:
                    logging.info("[%s] Joined room %s", self.matrix_client.user, room_id)
                    break

            if retries == 0:
                logging.error("[%s] Error joining room %s. Skipping...", self.matrix_client.user, room_id)
