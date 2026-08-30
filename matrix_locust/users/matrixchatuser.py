###########################################################
#
# matrixchatuser.py - The MatrixChatUser class
# -- Acts like a Matrix chat user
#
# Created: 2022-08-05
# Author: Charles V Wright <cvwright@futo.org>
# Copyright: 2022 FUTO Holdings Inc
# License: Apache License version 2.0
#
# The MatrixChatUser class extends MatrixUser to add some
# basic chatroom user behaviors.

# Upon login to the homeserver, this user spawns a second
# "background" Greenlet to act as the user's client's
# background sync task.  The "background" Greenlet sleeps and
# calls /sync in an infinite loop, and it uses the responses
# to /sync to populate the user's local understanding of the
# world state.
#
# Meanwhile, the user's main "foreground" Greenlet does the
# things that a Locust User normally does, sleeping and then
# picking a random @task to execute.  The available set of
# @tasks includes: accepting invites to join rooms, sending
# m.text messages, sending reactions, and paginating backward
# in a room.
#
###########################################################

import csv
import os
import sys
import glob
import random
import resource
import hashlib
import math

import json
import logging
import mimetypes
import time
from pathlib import Path
from urllib.parse import quote

import gevent
from locust import task, between, TaskSet
from locust import events
from locust.runners import MasterRunner, WorkerRunner

from matrix_locust.users.matrixuser import MatrixUser
from matrix_locust.data import USERS_CSV
from nio import MatrixRoom, RoomMessageText
from nio.responses import (
    LoginError,
    SyncError,
    RoomSendError,
    RoomMessagesError,
    ProfileSetDisplayNameError,
)

from typing import Optional

from nio.api import _FilterT


workload_stats = {}


def reset_workload_stats():
    global workload_stats
    workload_stats = {
        "text": {"attempts": 0, "successes": 0, "failures": 0,
                 "body_bytes": 0, "words": 0, "word_count_histogram": {}},
        "image": {"attempts": 0, "upload_successes": 0, "send_successes": 0,
                  "failures": 0, "uploaded_bytes": 0, "files": {}},
    }


def histogram_percentile(histogram, percentile):
    total = sum(histogram.values())
    if not total:
        return None
    target = math.ceil(total * percentile)
    cumulative = 0
    for value, count in sorted((int(value), count) for value, count in histogram.items()):
        cumulative += count
        if cumulative >= target:
            return value
    return None


reset_workload_stats()


# Preflight ###############################################

@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    random.seed(int(os.environ.get("MATRIX_EXPERIMENT_SEED", "42")))
    # Increase resource limits to prevent OS running out of descriptors
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except ValueError as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    # Multi-worker
    if isinstance(environment.runner, WorkerRunner):
        print(f"Registered 'load_users' handler on {environment.runner.client_id}")
        environment.runner.register_message("load_users", MatrixChatUser.load_users)
    # Single-worker
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(environment.runner, MasterRunner):
        # Open our list of users
        MatrixChatUser.worker_users = csv.DictReader(USERS_CSV.open(encoding="utf-8"))


@events.test_start.add_listener
def reset_stats_on_test_start(environment, **_kwargs):
    reset_workload_stats()


@events.test_stop.add_listener
def write_workload_stats(environment, **_kwargs):
    output = os.environ.get("MATRIX_WORKLOAD_STATS_PATH")
    if not output:
        return
    text = workload_stats["text"]
    histogram = text["word_count_histogram"]
    summary = {
        "configuration": {
            "workload": MatrixChatUser.workload,
            "message_rate": MatrixChatUser.message_rate,
            "image_ratio": MatrixChatUser.image_ratio,
            "text_length_profile": MatrixChatUser.text_length_profile,
            "text_length_words": MatrixChatUser.text_length_words,
            "experiment_seed": MatrixChatUser.experiment_seed,
            "sync_timeout_ms": MatrixChatUser.sync_timeout_ms,
        },
        **workload_stats,
        "text_length_summary": {
            "minimum_words": min(map(int, histogram), default=None),
            "average_words": text["words"] / text["attempts"] if text["attempts"] else None,
            "p95_words": histogram_percentile(histogram, 0.95),
            "maximum_words": max(map(int, histogram), default=None),
            "average_body_bytes": text["body_bytes"] / text["attempts"] if text["attempts"] else None,
        },
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

# Load our images and thumbnails
images_folder = "images"
image_files = glob.glob(os.path.join(images_folder, "*.jpg"))
images_with_thumbnails = []
for image_filename in image_files:
    image_basename = os.path.basename(image_filename)
    thumbnail_filename = os.path.join(images_folder, "thumbnails", image_basename)
    if os.path.exists(thumbnail_filename):
        images_with_thumbnails.append(image_filename)

# Find our user avatar images
avatars = []
avatars_folder = "avatars"
avatar_files = glob.glob(os.path.join(avatars_folder, "*.png"))

# Pre-generate some messages for the users to send
lorem_ipsum_text = """
Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.
"""
lorem_ipsum_words = lorem_ipsum_text.split()

###########################################################

class MatrixChatUser(MatrixUser):
    worker_id = None
    worker_users = []
    media_upload_supported = True

    # The experiment runner sets this explicitly for each factorial cell.
    workload = os.environ.get("MATRIX_WORKLOAD", "text_only").strip().lower()
    message_rate = float(os.environ.get("MATRIX_MESSAGE_RATE", "0.2"))
    image_ratio = float(os.environ.get("MATRIX_IMAGE_RATIO", "0.15"))
    text_length_profile = os.environ.get("MATRIX_TEXT_LENGTH_PROFILE", "fixed")
    text_length_words = int(os.environ.get("MATRIX_TEXT_LENGTH_WORDS", "10"))
    experiment_seed = int(os.environ.get("MATRIX_EXPERIMENT_SEED", "42"))
    sync_timeout_ms = int(os.environ.get("MATRIX_SYNC_TIMEOUT_MS", "30000"))
    text_task_weight = round((1.0 - image_ratio) * 1000)
    image_task_weight = round(image_ratio * 1000)

    @staticmethod
    def should_fallback_to_text_upload(status_code: int | None, payload: dict | None) -> bool:
        if status_code is None:
            return False
        if status_code < 400:
            return False
        if not isinstance(payload, dict):
            return False
        errcode = payload.get("errcode")
        error = str(payload.get("error") or "")
        if errcode == "M_UNRECOGNIZED" or "Unrecognized request" in error:
            return True
        return status_code == 404 and "upload" in str(payload).lower()

    @staticmethod
    def load_users(environment, msg, **_kwargs):
        MatrixChatUser.worker_users = iter(msg.data)
        MatrixChatUser.worker_id = environment.runner.client_id
        logging.info("Worker [%s] Received %s users", environment.runner.client_id, len(msg.data))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.room_messages = {}
        self.recent_messages = {}
        self.earliest_sync_tokens = {}
        self.user_avatar_urls = {}
        self.user_display_names = {}
        self.matrix_sync_task = None
        self.initial_sync_token = None
        self._next_action_at = time.monotonic()
        self._rng = random.Random(self.experiment_seed)

        self.matrix_client.add_event_callback(self.message_callback, RoomMessageText)

    def wait_time(self):
        """Random pacing with an exponential inter-arrival distribution.

        A non-positive rate selects maximum-throughput mode. With pacing, the
        expected interval is 1 / message_rate seconds per user.
        """
        if self.message_rate <= 0:
            return 0
        self._next_action_at += self._rng.expovariate(self.message_rate)
        now = time.monotonic()
        if self._next_action_at < now:
            self._next_action_at = now
            return 0
        return self._next_action_at - now

    def on_start(self):
        # Load the next user who needs to be logged-in
        try:
            user = next(MatrixChatUser.worker_users)
        except StopIteration:
            gevent.sleep(999999)
            return

        # Change to force user login request and refresh tokens
        invalidate_access_tokens = False

        self.login_from_csv(user)

        identity = self.matrix_client.user or user.get("username", "unknown")
        seed_material = f"{self.experiment_seed}:{identity}".encode("utf-8")
        self._rng.seed(int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big"))

        if self.matrix_client.user is None or self.matrix_client.password is None:
            logging.error("Couldn't get username/password. Skipping...")
            return

        if invalidate_access_tokens:
            self.matrix_client.user_id = None
            self.matrix_client.access_token = None

        # Log in as this current user if not already logged in
        if self.matrix_client.user_id is None or self.matrix_client.access_token is None or \
            len(self.matrix_client.user_id) < 1 or len(self.matrix_client.access_token) < 1:

            while True:
                response = self.matrix_client.login(self.matrix_client.password)

                if isinstance(response, LoginError):
                    logging.error("Login failed for User [%s]", self.matrix_client.user)
                    return
                else:
                    break

        # Spawn a Greenlet to act as this user's client, constantly /sync'ing with the server
        self.matrix_sync_task = gevent.spawn(
            self.sync_forever, client_sleep=None, timeout=self.sync_timeout_ms
        )

        # Wait a bit before we take our first action
        self.wait()

    def on_stop(self):
        pass
        # Currently we don't want to invalidate access tokens stored in the csv file
        # self.logout()

    def get_random_roomid(self):
        if len(self.matrix_client.rooms) > 0:
            room_id = self._rng.choice(list(self.matrix_client.rooms.keys()))
            return room_id
        else:
            return None


    def load_data_for_room(self, room_id):
        # FIXME Need to parse the room state for all of this :-\
        ## FIXME Load the room displayname and avatar url
        ## FIXME If we don't have it, load the avatar image
        #room_displayname = self.room_display_names.get(room_id, None)
        #if room_displayname is None:
        #  # Uh-oh, do we need to parse the room state from /sync in order to get this???
        #  pass
        #room_avatar_url = self.room_avatar_urls.get(room_id, None)
        #if room_avatar_url is None:
        #  # Uh-oh, do we need to parse the room state from /sync in order to get this???
        #  pass
        ## Note: We may have just set room_avatar_url in the code above
        #if room_avatar_url is not None and self.media_cache.get(room_avatar_url, False) is False:
        #  # FIXME Download the image and set the cache to True
        #  pass

        # Load the avatars for recent users
        # Load the thumbnails for any messages that have one
        messages = self.recent_messages.get(room_id, [])

        for message in messages:
            sender_userid = message.sender
            sender_avatar_mxc = self.user_avatar_urls.get(sender_userid, None)
            if sender_avatar_mxc is None:
                # FIXME Fetch the avatar URL for sender_userid
                # FIXME Set avatar_mxc
                # FIXME Set self.user_avatar_urls[sender_userid]
                self.matrix_client.get_avatar(sender_userid)
            # Try again.  Maybe we were able to populate the cache in the line above.
            sender_avatar_mxc = self.user_avatar_urls.get(sender_userid, None)
            # Now avatar_mxc might not be None, even if it was above
            if sender_avatar_mxc is not None and len(sender_avatar_mxc) > 0:
                # FIXME Reimplement method with nio after avatar support is added
                self.download_matrix_media(sender_avatar_mxc)
            sender_displayname = self.user_display_names.get(sender_userid, None)
            if sender_displayname is None:
                sender_displayname = self.matrix_client.get_displayname(sender_userid)

        # Currently users only send text messages
        # for message in messages:
        #     content = message.content
        #     msgtype = content.msgtype
        #     if msgtype in ["m.image", "m.video", "m.file"]:
        #         thumb_mxc = message.content.get("thumbnail_url", None)
        #         if thumb_mxc is not None:
        #             self.download_matrix_media(thumb_mxc)


    def sync_forever(
        self,
        client_sleep: Optional[float] = None,
        timeout: Optional[int] = None,
        sync_filter: _FilterT = None,
        since: Optional[str] = None,
        full_state: Optional[bool] = None,
        set_presence: Optional[str] = None,
    ):
        # client_sleep is in seconds

        # Continually call the /sync endpoint
        # Put anything that the user might care about into our instance variables where the
        # user @task's can find it
        while True:
            response = self.matrix_client.sync(timeout, sync_filter, since, full_state, set_presence)

            if isinstance(response, SyncError):
                logging.error("[%s] /sync error (%s): %s",
                              self.matrix_client.user, response.status_code, response.message)
                # Avoid a tight retry loop that can generate thousands of
                # failed requests per second during DNS/network failures.
                gevent.sleep(1)
            else:
                if self.initial_sync_token is None:
                    self.initial_sync_token = response.next_batch

            if not(client_sleep is None):
                gevent.sleep(client_sleep)

    def message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        # Add the new messages to whatever we had before (if anything)
        if self.room_messages.get(room.room_id) is None:
            self.room_messages[room.room_id] = []
        self.room_messages[room.room_id].append(event)

        # Store only the most recent 10 messages, regardless of how many we had before or how many we just received
        self.recent_messages[room.room_id] = self.room_messages[room.room_id][-10:]


    # @task(23)
    # def do_nothing(self):
    #     self.wait()

    def text_word_count(self):
        """Return a reproducible word count for the configured profile."""
        profiles = {
            "short": (math.log(3), 1.0, 69),
            "mixed": (math.log(10), 0.9, 200),
            "long": (math.log(30), 0.9, 400),
        }
        if self.text_length_profile == "fixed":
            return self.text_length_words
        if self.text_length_profile not in profiles:
            raise ValueError(f"Unknown MATRIX_TEXT_LENGTH_PROFILE={self.text_length_profile!r}")
        mean, sigma, maximum = profiles[self.text_length_profile]
        return max(1, min(round(self._rng.lognormvariate(mean, sigma)), maximum))

    def generate_message(self, word_count):
        """Vary content while preserving the requested number of words."""
        return " ".join(self._rng.choices(lorem_ipsum_words, k=word_count))

    @task(text_task_weight)
    def send_text(self):
        room_id = self.get_random_roomid()
        if room_id is None:
            #logging.warning("User [%s] couldn't get a room for send_text" % self.username)
            return
        #logging.info("User [%s] sending a message to room [%s]" % (self.username, room_id))

        # Send the typing notification like a real client would
        # self.matrix_client.room_typing(room_id, True)
        # Sleep while we pretend the user is banging on the keyboard
        # delay = random.expovariate(1.0 / 5.0)
        # gevent.sleep(delay)

        message_len = self.text_word_count()
        message_text = self.generate_message(message_len)
        body_bytes = len(message_text.encode("utf-8"))
        text_stats = workload_stats["text"]
        text_stats["attempts"] += 1
        text_stats["words"] += message_len
        text_stats["body_bytes"] += body_bytes
        histogram = text_stats["word_count_histogram"]
        histogram[str(message_len)] = histogram.get(str(message_len), 0) + 1
        message_content = {
            "msgtype": "m.text",
            "body": message_text,
        }

        response = self.matrix_client.room_send(
            room_id, "m.room.message", message_content,
            request_name="/_matrix/client/v3/rooms/_/send/m.text",
        )
        if isinstance(response, RoomSendError):
            text_stats["failures"] += 1
            logging.error("[%s] failed to send m.text to room [%s]", self.matrix_client.user, room_id)
        else:
            text_stats["successes"] += 1

    @task(image_task_weight)
    def send_image(self):
        """Upload an image and send the resulting MXC URI to a room.

        In the text-only factorial cells this task deliberately becomes a
        text send. This keeps the total task scheduling weight identical in
        both workloads, avoiding a workload-dependent think-time artifact.
        """
        if self.workload == "text_only":
            self.send_text()
            return
        if self.workload != "text_and_image":
            logging.error("Unknown MATRIX_WORKLOAD=%r", self.workload)
            return

        if not self.media_upload_supported:
            self.send_text()
            return

        room_id = self.get_random_roomid()
        if room_id is None or not image_files:
            if not image_files:
                logging.error("No JPG test images found under images/")
            return

        image_path = self._rng.choice(image_files)
        image_stats = workload_stats["image"]
        image_stats["attempts"] += 1
        filename = os.path.basename(image_path)
        image_stats["files"][filename] = image_stats["files"].get(filename, 0) + 1
        content_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        upload_path = (
            "/_matrix/media/v3/upload?filename="
            + quote(os.path.basename(image_path))
        )
        headers = {
            "Authorization": f"Bearer {self.matrix_client.access_token}",
            "Content-Type": content_type,
        }
        try:
            with open(image_path, "rb") as image_file:
                image_data = image_file.read()
            with self.rest(
                "POST",
                upload_path,
                headers=headers,
                data=image_data,
                name="/_matrix/media/v3/upload",
            ) as upload_response:
                upload_json = upload_response.js or {}
                if self.should_fallback_to_text_upload(upload_response.status_code, upload_json):
                    self.__class__.media_upload_supported = False
                    upload_response.success()
                    logging.warning(
                        "[%s] media upload endpoint rejected by homeserver (%s): %s; "
                        "falling back to text-only workload",
                        self.matrix_client.user,
                        upload_response.status_code,
                        upload_json,
                    )
                    self.send_text()
                    return
                mxc_uri = upload_json.get("content_uri")
                if upload_response.status_code >= 400 or not mxc_uri:
                    image_stats["failures"] += 1
                    upload_response.failure("Matrix image upload failed")
                    return
                image_stats["upload_successes"] += 1
                image_stats["uploaded_bytes"] += len(image_data)
        except OSError as exc:
            image_stats["failures"] += 1
            logging.error("Could not read image %s: %s", image_path, exc)
            return

        message_content = {
            "msgtype": "m.image",
            "body": os.path.basename(image_path),
            "url": mxc_uri,
            "info": {
                "mimetype": content_type,
                "size": len(image_data),
            },
        }
        response = self.matrix_client.room_send(
            room_id, "m.room.message", message_content,
            request_name="/_matrix/client/v3/rooms/_/send/m.image",
        )
        if isinstance(response, RoomSendError):
            image_stats["failures"] += 1
            logging.error("[%s] failed to send m.image to room [%s]", self.matrix_client.user, room_id)
        else:
            image_stats["send_successes"] += 1

    # @task(4)
    # def look_at_room(self):
    #     room_id = self.get_random_roomid()
    #     if room_id is None:
    #         #logging.warning("User [%s] couldn't get a roomid for look_at_room" % self.username)
    #         return
    #     #logging.info("User [%s] looking at room [%s]" % (self.username, room_id))

    #     self.load_data_for_room(room_id)

    #     if len(self.recent_messages.get(room_id, [])) < 1:
    #         return

    #     event_id = self.recent_messages[room_id][-1].event_id
    #     self.matrix_client.update_receipt_marker(room_id, event_id)


    # # FIXME Combine look_at_room() and paginate_room() into a TaskSet,
    # #       so the user can paginate and scroll the room for a longer
    # #       period of time.
    # #       In this model, we should load the displaynames and avatars
    # #       and message thumbnails every time we paginate, just like a
    # #       real client would do as the user scrolls the timeline.
    # @task
    # def paginate_room(self):
    #     room_id = self.get_random_roomid()
    #     token = self.earliest_sync_tokens.get(room_id, self.initial_sync_token)
    #     if room_id is None or token is None:
    #         return

    #     response = self.matrix_client.room_messages(room_id, token)
    #     if isinstance(response, RoomMessagesError):
    #         logging.error("[%s] failed /messages failed for room [%s]", self.matrix_client.user, room_id)
    #     else:
    #         self.earliest_sync_tokens[room_id] = response.end

    # @task(1)
    # def go_afk(self):
    #     logging.info("[%s] going away from keyboard", self.matrix_client.user)
    #     # Generate large(ish) random away time
    #     away_time = random.expovariate(1.0 / 600.0)  # Expected value = 10 minutes
    #     gevent.sleep(away_time)

    # @task(1)
    # def change_displayname(self):
    #     user_number = self.matrix_client.user.split(".")[-1]
    #     random_number = random.randint(1,1000)
    #     new_name = "User %s (random=%d)" % (user_number, random_number)

    #     response = self.matrix_client.set_displayname(new_name)
    #     if isinstance(response, ProfileSetDisplayNameError):
    #         logging.error("[%s] failed to set displayname to %s: Code=%s, Message=%s",
    #                       self.matrix_client.user, new_name, response.status_code, response.message)


    # @task(3)
    # class ChatInARoom(TaskSet):

    #     def wait_time(self):
    #         expected_wait = 25.0
    #         rate = 1.0 / expected_wait
    #         return random.expovariate(rate)

    #     def on_start(self):
    #         #logging.info("User [%s] chatting in a room" % self.user.username)
    #         if len(self.user.matrix_client.rooms.keys()) == 0:
    #             self.interrupt()
    #         else:
    #             self.room_id = self.user.get_random_roomid()
    #             self.reacted_messages = []

    #             if self.room_id is None:
    #                 self.interrupt()
    #             else:
    #                 self.user.load_data_for_room(self.room_id)

    #     @task
    #     def send_text(self):
    #         # Send the typing notification like a real client would
    #         self.user.matrix_client.room_typing(self.room_id, True)
    #         # Sleep while we pretend the user is banging on the keyboard
    #         delay = random.expovariate(1.0 / 5.0)
    #         gevent.sleep(delay)

    #         message_len = round(random.lognormvariate(1.0, 1.0))
    #         message_len = min(message_len, len(lorem_ipsum_words))
    #         message_len = max(message_len, 1)
    #         message_text = lorem_ipsum_messages[message_len]
    #         message_content = {
    #             "msgtype": "m.text",
    #             "body": message_text,
    #         }

    #         response = self.user.matrix_client.room_send(self.room_id, "m.room.message", message_content)
    #         if isinstance(response, RoomSendError):
    #             logging.error("[%s] failed to send/chat in room [%s]", self.user.matrix_client.user, self.room_id)

    #     @task
    #     def send_image(self):
    #         # Choose an image to send/upload
    #         # Upload the thumbnail -- FIXME We need to have all of the thumbnails created and stored *before* we start the test.  Performance will be awful if we're trying to dynamically resample the images on-the-fly here in the load generator.
    #         # Upload the image data, get back an MXC URL
    #         # Craft the event JSON structure
    #         # Send the event
    #         pass


    #     @task
    #     def send_reaction(self):
    #         # Pick a recent message from the selected room,
    #         # and react to it
    #         if len(self.user.recent_messages.get(self.room_id, [])) < 1:
    #             return

    #         message = random.choice(self.user.recent_messages[self.room_id])
    #         reaction = random.choice(["💩","👍","❤️", "👎", "🤯", "😱", "👏"])
    #         content = {
    #             "m.relates_to": {
    #                 "rel_type": "m.annotation",
    #                 "event_id": message.event_id,
    #                 "key": reaction,
    #             }
    #         }

    #         # Prevent errors with reacting to the same message with the same reaction
    #         if (message, reaction) in self.reacted_messages:
    #             return
    #         else:
    #             self.reacted_messages.append((message, reaction))

    #         # logging.info("[%s] sending reaction %s to message %s in room %s with event %s",
    #         #              self.user.matrix_client.user, reaction, message, self.room_id, message.event_id)
    #         response = self.user.matrix_client.room_send(self.room_id, "m.reaction", content)
    #         if isinstance(response, RoomSendError):
    #             logging.error("[%s] failed to send reaction in room [%s]: Code=%s, Message=%s",
    #                           self.user.matrix_client.user, response.room_id, response.status_code, response.message)


    #     @task
    #     def stop(self):
    #         #logging.info("User [%s] stopping chat in room [%s]" % (self.user.username, self.room_id))
    #         self.interrupt()

    #     # Each time we create a new instance of this task, we want to have the user
    #     # generate a slightly different expected number of messages.
    #     # FIXME Hmmm this doesn't seem to work...
    #     tasks = {
    #         send_text: max(1, round(random.gauss(15,4))),
    #         send_image: random.choice([0,0,0,1,1,2]),
    #         send_reaction: random.choice([0,0,1,1,1,2,3]),
    #         stop: 1,
    #     }
