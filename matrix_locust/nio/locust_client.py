# -*- coding: utf-8 -*-

# # Copyright © 2018, 2019 Damir Jelić <poljar@termina.org.uk>
# #
# # Permission to use, copy, modify, and/or distribute this software for
# # any purpose with or without fee is hereby granted, provided that the
# # above copyright notice and this permission notice appear in all copies.
# #
# # THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# # WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# # MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# # SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# # RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# # CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# # CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import json
import pprint
from builtins import str, super
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from typing import (
    Any,
    AsyncIterable,
    BinaryIO,
    Callable,
    Coroutine,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)
from uuid import UUID, uuid4

import nio.api
from nio.api import (
    Api,
    EventFormat,
    MessageDirection,
    PushRuleKind,
    ResizingMethod,
    RoomPreset,
    RoomVisibility,
    _FilterT,
)
from nio.events import MegolmEvent
# from nio.log import logger_group
from nio.responses import (
    DeleteDevicesAuthResponse,
    DeleteDevicesResponse,
    DevicesResponse,
    DownloadResponse,
    ErrorResponse,
    FileResponse,
    JoinedMembersResponse,
    JoinError,
    JoinResponse,
    KeysClaimResponse,
    KeysQueryResponse,
    KeysUploadError,
    KeysUploadResponse,
    LoginInfoResponse,
    LoginError,
    LoginResponse,
    LogoutError,
    LogoutResponse,
    ProfileGetAvatarError,
    ProfileGetAvatarResponse,
    ProfileGetDisplayNameError,
    ProfileGetDisplayNameResponse,
    ProfileGetResponse,
    ProfileSetAvatarError,
    ProfileSetAvatarResponse,
    ProfileSetDisplayNameError,
    ProfileSetDisplayNameResponse,
    Response,
    RoomCreateError,
    RoomCreateResponse,
    RoomForgetResponse,
    RoomGetStateEventError,
    RoomGetStateEventResponse,
    RoomGetStateError,
    RoomGetStateResponse,
    RoomInviteResponse,
    RoomKeyRequestResponse,
    RoomKickResponse,
    RoomLeaveResponse,
    RoomMessagesError,
    RoomMessagesResponse,
    RoomPutStateError,
    RoomPutStateResponse,
    RoomReadMarkersResponse,
    RoomRedactResponse,
    RoomSendResponse,
    RoomTypingError,
    RoomTypingResponse,
    ShareGroupSessionResponse,
    SyncError,
    SyncResponse,
    ThumbnailResponse,
    ToDeviceResponse,
    UpdateDeviceResponse,
    UpdateReceiptMarkerResponse,
)
from nio.client import Client, ClientConfig
from nio.client.base_client import logged_in, store_loaded

import logging
from locust import User
from http import HTTPStatus
from collections import namedtuple

from matrix_locust.nio.contrib import (
    ApiExt,
    RoomGetTagsError,
    RoomGetTagsResponse,
    RoomSetTagsError,
    RoomSetTagsResponse,
)

@dataclass
class ResponseCb:
    """Response callback."""

    func: Callable = field()
    filter: Union[Tuple[Type], Type, None] = None


class LocustClient(Client):
    """Matrix no-IO client.

    Attributes:
       access_token (str): Token authorizing the user with the server. Is set
           after logging in.
       user_id (str): The full mxid of the current user. This is set after
           logging in.
       next_batch (str): The current sync token.
       rooms (Dict[str, MatrixRoom)): A dictionary containing a mapping of room
           ids to MatrixRoom objects. All the rooms a user is joined to will be
           here after a sync.
       invited_rooms (Dict[str, MatrixInvitedRoom)): A dictionary containing
           a mapping of room ids to MatrixInvitedRoom objects. All the rooms
           a user is invited to will be here after a sync.

    Args:
       user (str, optional): The user which will be used when we log in to the
           homeserver.
       device_id (str, optional): An unique identifier that distinguishes
           this client instance. If not set the server will provide one after
           log in.
       store_dir (str, optional): The directory that should be used for state
           storage.
       config (ClientConfig, optional): Configuration for the client.

    """

    def __init__(
        self,
        locust_user: User,
        user: str = "",
        device_id: Optional[str] = "",
        store_path: Optional[str] = "",
        config: Optional[ClientConfig] = None,
    ):
        self.locust_user = locust_user
        self.response_callbacks: List[ResponseCb] = []

        self.password = ""
        self.matrix_domain = None

        super().__init__(user, device_id, store_path, config)

    def _build_request(self, api_response):
        """Preserve the versioned paths selected by the installed matrix-nio.

        Older project revisions rewrote private ``nio.api`` constants here.
        matrix-nio 0.26 already emits the current Client and Media API paths,
        and those private constant names are not part of its stable API.
        """
        return tuple(api_response)

    def _send(self,
              response: Response,
              method: str,
              url: str,
              body: str = None,
              name: str = None,
              *response_data,
    ):
        headers = {
            'Content-Type': 'application/json', 'Accept': 'application/json',
        }

        if body is not None:
            body = json.loads(body)

        # Strip out url parameters from Locust logs
        if name is None and "?" in url:
            name = url[:url.find("?")]

        # Send request and update internal state of the object with the response
        # logging.info("[%s] Making API call to %s" % (self.user, url))
        with self.locust_user.rest(method, url, headers=headers, json=body, name=name) as resp:
            # Handle empty or invalid responses
            response_data_dict = resp.js if resp.js is not None else {}

            # Check for HTTP error status codes
            if resp.status_code >= 400:
                logging.warning("[%s] HTTP %d error from %s: %s",
                               self.user, resp.status_code, url, response_data_dict)

            try:
                matrix_response = response.from_dict(response_data_dict, *response_data)
            except (TypeError, ValueError) as e:
                logging.error("[%s] Failed to parse response from %s: %s (status: %d)",
                             self.user, url, str(e), resp.status_code)
                # Return an error response if parsing fails
                if response == SyncResponse:
                    matrix_response = SyncError(message=f"Invalid response: {str(e)}",
                                               status_code=resp.status_code)
                elif response == LoginResponse:
                    matrix_response = LoginError(message=f"Invalid response: {str(e)}")
                else:
                    # Generic error response for unknown response types
                    matrix_response = ErrorResponse(message=f"Invalid response: {str(e)}",
                                                   transport_response=resp)

            self.receive_response(matrix_response)
            self.run_response_callbacks([matrix_response])
            return matrix_response


    def add_response_callback(
        self,
        func: Coroutine[Any, Any, Response],
        cb_filter: Union[Tuple[Type], Type, None] = None,
    ):
        """Add a coroutine that will be called if a response is received.

        Args:
            func (Coroutine): The coroutine that will be called with the
                response as the argument.
            cb_filter (Type, optional): A type or a tuple of types for which
                the callback should be called.

        Example:

            >>> # A callback that will be called every time our `sync_forever`
            >>> # method successfully syncs with the server.
            >>> async def sync_cb(response):
            ...    print(f"We synced, token: {response.next_batch}")
            ...
            >>> client.add_response_callback(sync_cb, SyncResponse)
            >>> await client.sync_forever(30000)

        """
        cb = ResponseCb(func, cb_filter)  # type: ignore
        self.response_callbacks.append(cb)

    def run_response_callbacks(
        self, responses: List[Union[Response, ErrorResponse]]
    ):
        """Run the configured response callbacks for the given responses.

        Low-level function which is normally only used by other methods of
        this class. Automatically called by sync_forever() and all functions
        calling receive_response().
        """
        for response in responses:
            for cb in self.response_callbacks:
                if cb.filter is None or isinstance(response, cb.filter):
                    cb.func(response)

    def login(
        self,
        password: Optional[str] = None,
        device_name: Optional[str] = "",
        token: Optional[str] = None,
    ) -> Union[LoginResponse, LoginError]:
        """Login to the homeserver.

        Calls receive_response() to update the client state if necessary.

        Args:
            password (str, optional): The user's password.
            device_name (str): A display name to assign to a newly-created
                device. Ignored if the logged in device corresponds to a
                known device.
            token (str, optional): A login token, for example provided by a
                single sign-on service.

        Either a password or a token needs to be provided.

        Returns either a `LoginResponse` if the request was successful or
        a `LoginError` if there was an error with the request.
        """

        if password is None and token is None:
            raise ValueError("Either a password or a token needs to be provided")

        method, path, data = self._build_request(Api.login(
            self.user,
            password=password,
            device_name=device_name,
            device_id=self.device_id,
            token=token,
        ))

        self.password = password
        response = self._send(LoginResponse, method, path, data)

        if isinstance(response, LoginResponse):
            self.matrix_domain = self.user_id.split(":")[-1]

        return response

    @logged_in
    def logout(
        self, all_devices: bool = False
    ) -> Union[LogoutResponse, LogoutError]:
        """Logout from the homeserver.

        Calls receive_response() to update the client state if necessary.

        Returns either 'LogoutResponse' if the request was successful or
        a `Logouterror` if there was an error with the request.
        """
        method, path, data = self._build_request(Api.logout(self.access_token, all_devices))

        response = self._send(LogoutResponse, method, path, data)

        if isinstance(response, LogoutResponse):
            self.user_id = None
            self.device_id = None
            self.matrix_domain = None

        return response

    def register(
            self,
            username,
            password,
            device_name: Optional[str] = "",
            token: Optional[str] = ""
    ):
        """Register with homeserver.

        Calls receive_response() to update the client state if necessary.

        Args:
            username (str): Username to register the new user as.
            password (str): New password for the user.
            device_name (str): A display name to assign to a newly-created
                device. Ignored if the logged in device corresponds to a
                known device.
            token (str): Optional registration token

        Returns a 'RegisterResponse' if successful.
        """
        method, path, data = self._build_request(Api.register(
            user=username,
            password=password,
            device_name=device_name,
            device_id=self.device_id,
        ))

        data = json.loads(data)

        with self.locust_user.rest(method, path, json=data) as response1:
            if response1.status_code == HTTPStatus.OK: #200
                logging.info("User [%s] Success!  Didn't even need UIAA!", self.user)
                self.user_id = response1.js.get("user_id", None)
                self.access_token = response1.js.get("access_token", None)
                self.matrix_domain = self.user_id.split(":")[-1]
                if self.user_id is None or self.access_token is None:
                    logging.error("User [%s] Failed to parse /register response!\nResponse: %s", self.user, response1.js)
                    return
                self.locust_user.update_tokens()
            elif response1.status_code == HTTPStatus.UNAUTHORIZED: #401
                # Not an error, unauthorized requests are apart of the registration-flow
                response1.success()

                flows = response1.js.get("flows", None)
                if flows is None:
                    logging.error("User [%s] No UIAA flows for /register\nResponse: %s", self.user, response1.js)
                    self.locust_user.environment.runner.quit()
                    return

                session_id = response1.js.get("session", None)
                if session_id is None:
                    logging.info("User [%s] No session ID provided by server for /register", self.user)
                else:
                    data["auth"]["session"] = session_id

                # Pick the first available login flow and attempt to use it
                for flow in flows:
                    stages = flow.get("stages", [])
                    if len(stages) > 0:
                        logging.info(f"Found UIAA flow [{', '.join(stages)}]")

                        for stage in stages:
                            data["auth"]["type"] = stage

                            if stage == "m.login.dummy":
                                pass
                            elif stage == "m.login.password":
                                data["auth"]["identifier"]["type"] = "m.id.user"
                                data["auth"]["identifier"]["user"] = self.user
                                data["auth"]["password"] = self.password
                            elif stage == "m.login.registration_token":
                                data["auth"]["token"] = token

                            with self.locust_user.rest("POST", path, json=data) as response2:
                                print(response2.js)
                                if response2.status_code == HTTPStatus.OK or response2.status_code == HTTPStatus.CREATED: # 200 or 201
                                    logging.info("User [%s] Success!", self.user)
                                    self.user_id = response2.js.get("user_id", None)
                                    self.access_token = response2.js.get("access_token", None)
                                    self.matrix_domain = self.user_id.split(":")[-1]
                                    if self.user_id is None or self.access_token is None:
                                        logging.error("User [%s] Failed to parse /register response!\nResponse: %s", self.user,
                                                    response2.js)
                                        return
                                    self.locust_user.update_tokens()
                                    return
                                elif response2.status_code == HTTPStatus.UNAUTHORIZED: #401
                                    continue
                                else:
                                    logging.error("User[%s] /register failed with status code %d\nResponse: %s", self.user,
                                            response2.status_code, response2.js)
                                    break
            else:
                logging.error("User[%s] /register failed with status code %d\nResponse: %s", self.user,
                            response1.status_code, response1.js)


        #return await self._send(RegisterResponse, method, path, data)

    @logged_in
    def room_send(
        self,
        room_id: str,
        message_type: str,
        content: Dict[Any, Any],
        tx_id: Optional[str] = None,
        ignore_unverified_devices: bool = False,
        request_name: Optional[str] = None,
    ):
        """Send a message to a room.

        Calls receive_response() to update the client state if necessary.

        Args:
            room_id(str): The room id of the room where the message should be
                sent to.
            message_type(str): A string identifying the type of the message.
            content(Dict[Any, Any]): A dictionary containing the content of the
                message.
            tx_id(str, optional): The transaction ID of this event used to
                uniquely identify this message.
            ignore_unverified_devices(bool): If the room is encrypted and
                contains unverified devices, the devices can be marked as
                ignored here. Ignored devices will still receive encryption
                keys for messages but they won't be marked as verified.

        If the room where the message should be sent is encrypted the message
        will be encrypted before sending.

        This method also makes sure that the room members are fully synced and
        that keys are queried before sending messages to an encrypted room.

        If the method can't sync the state fully to send out an encrypted
        message after a couple of retries it raises `SendRetryError`.

        Raises `LocalProtocolError` if the client isn't logged in.
        """
        uuid: Union[str, UUID] = tx_id or uuid4()

        # if self.olm:
        #     try:
        #         room = self.rooms[room_id]
        #     except KeyError:
        #         raise LocalProtocolError(f"No such room with id {room_id} found.")

        #     if room.encrypted:
        #         # Check if the members are synced, otherwise users might not get
        #         # the megolm seession.
        #         if not room.members_synced:
        #             responses = []
        #             responses.append(await self.joined_members(room_id))

        #             if self.should_query_keys:
        #                 responses.append(await self.keys_query())

        #         # Check if we need to share a group session, it might have been
        #         # invalidated or expired.
        #         if self.olm.should_share_group_session(room_id):
        #             try:
        #                 event = self.sharing_session[room_id]
        #                 await event.wait()
        #             except KeyError:
        #                 await self.share_group_session(
        #                     room_id,
        #                     ignore_unverified_devices=ignore_unverified_devices,
        #                 )

        #         # Reactions as of yet don't support encryption.
        #         # Relevant spec proposal https://github.com/matrix-org/matrix-doc/pull/1849
        #         if message_type != "m.reaction":
        #             # Encrypt our content and change the message type.
        #             message_type, content = self.encrypt(room_id, message_type, content)

        method, path, data = self._build_request(Api.room_send(
            self.access_token, room_id, message_type, content, uuid
        ))
        label = request_name or f"/_matrix/client/v3/rooms/_/send/{message_type}/_"
        return self._send(RoomSendResponse, method, path, data, label, (room_id,))

    @logged_in
    def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: Dict[Any, Any],
        state_key: str = "",
    ) -> Union[RoomPutStateResponse, RoomPutStateError]:
        """Send a state event to a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomPutStateResponse` if the request was successful
        or a `RoomPutStateError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room to send the event to.
            event_type (str): The type of the state to send.
            content (Dict[Any, Any]): The content of the event to be sent.
            state_key (str): The key of the state event to send.
        """

        method, path, data = Api.room_put_state(
            self.access_token,
            room_id,
            event_type,
            content,
            state_key=state_key,
        )

        label = f"/_matrix/client/v3/rooms/_/state/{event_type}/_"
        return self._send(RoomPutStateResponse, method, path, data, label, (room_id,))

    @logged_in
    def room_get_state(
        self,
        room_id: str,
    ) -> Union[RoomGetStateResponse, RoomGetStateError]:
        """Fetch state for a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomGetStateResponse` if the request was successful
        or a `RoomGetStateError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room to fetch state from.
        """

        method, path = Api.room_get_state(
            self.access_token,
            room_id,
        )
        label = "/_matrix/client/v3/rooms/_/state/"
        return self._send(RoomGetStateResponse, method, path, None, label, (room_id,))

    @logged_in
    def room_get_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> Union[RoomGetStateEventResponse, RoomGetStateEventError]:
        """Fetch a state event from a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomGetStateEventResponse` if the request was
        successful or a `RoomGetStateEventError` if there was an error with
        the request.

        Args:
            room_id (str): The room id of the room to fetch the event from.
            event_type (str): The type of the state to fetch.
            state_key (str): The key of the state event to fetch.
        """

        method, path = Api.room_get_state_event(
            self.access_token, room_id, event_type, state_key=state_key
        )

        label = f"/_matrix/client/v3/rooms/_/state/{event_type}/_"
        return self._send(RoomGetStateEventResponse,
            method,
            path,
            None,
            label,
            (
                event_type,
                state_key,
                room_id,
            ),
        )

    @logged_in
    def room_create(
        self,
        visibility: RoomVisibility = RoomVisibility.private,
        alias: Optional[str] = None,
        name: Optional[str] = None,
        topic: Optional[str] = None,
        room_version: Optional[str] = None,
        federate: bool = False,
        is_direct: bool = False,
        preset: Optional[RoomPreset] = None,
        invite: Sequence[str] = (),
        initial_state: Sequence[Dict[str, Any]] = (),
        power_level_override: Optional[Dict[str, Any]] = None,
        predecessor: Optional[Dict[str, Any]] = None,
        space: bool = False,
    ) -> Union[RoomCreateResponse, RoomCreateError]:
        """Create a new room.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            visibility (RoomVisibility): whether to have the room published in
                the server's room directory or not.
                Defaults to ``RoomVisibility.private``.

            alias (str, optional): The desired canonical alias local part.
                For example, if set to "foo" and the room is created on the
                "example.com" server, the room alias will be
                "#foo:example.com".

            name (str, optional): A name to set for the room.

            topic (str, optional): A topic to set for the room.

            room_version (str, optional): The room version to set.
                If not specified, the homeserver will use its default setting.
                If a version not supported by the homeserver is specified,
                a 400 ``M_UNSUPPORTED_ROOM_VERSION`` error will be returned.

            federate (bool): Whether to allow users from other homeservers from
                joining the room. Defaults to ``False``.
                Cannot be changed later.

            is_direct (bool): If this should be considered a
                direct messaging room.
                If ``True``, the server will set the ``is_direct`` flag on
                ``m.room.member events`` sent to the users in ``invite``.
                Defaults to ``False``.

            preset (RoomPreset, optional): The selected preset will set various
                rules for the room.
                If unspecified, the server will choose a preset from the
                ``visibility``: ``RoomVisibility.public`` equates to
                ``RoomPreset.public_chat``, and
                ``RoomVisibility.private`` equates to a
                ``RoomPreset.private_chat``.

            invite (list): A list of user id to invite to the room.

            initial_state (list): A list of state event dicts to send when
                the room is created.
                For example, a room could be made encrypted immediately by
                having a ``m.room.encryption`` event dict.

            power_level_override (dict): A ``m.room.power_levels content`` dict
                to override the default.
                The dict will be applied on top of the generated
                ``m.room.power_levels`` event before it is sent to the room.

            predecessor (dict): A reference to the room this room replaces, if the previous room was upgraded.
                Containing the event ID of the last known event in the old room.
                And the ID of the old room.
                ``event_id``: ``$something:example.org``,
                ``room_id``: ``!oldroom:example.org``

            space (bool): Create as a Space (defaults to False).
        """

        method, path, data = self._build_request(Api.room_create(
            self.access_token,
            visibility=visibility,
            alias=alias,
            name=name,
            topic=topic,
            room_version=room_version,
            federate=federate,
            is_direct=is_direct,
            preset=preset,
            invite=invite,
            initial_state=initial_state,
            power_level_override=power_level_override,
            predecessor=predecessor,
            space=space,
        ))

        return self._send(RoomCreateResponse, method, path, body=data)

    @logged_in
    def join(self, room_id: str) -> Union[JoinResponse, JoinError]:
        """Join a room.

        This tells the server to join the given room.
        If the room is not public, the user must be invited.

        Calls receive_response() to update the client state if necessary.

        Returns either a `JoinResponse` if the request was successful or
        a `JoinError` if there was an error with the request.

        Args:
            room_id: The room id or alias of the room to join.
        """
        method, path, data = self._build_request(Api.join(self.access_token, room_id))

        label = "/_matrix/client/v3/join/_"
        return self._send(JoinResponse, method, path, body=data, name=label)

    @logged_in
    def room_messages(
        self,
        room_id: str,
        start: str,
        end: Optional[str] = None,
        direction: MessageDirection = MessageDirection.back,
        limit: int = 10,
        message_filter: Optional[Dict[Any, Any]] = None,
    ) -> Union[RoomMessagesResponse, RoomMessagesError]:
        """Fetch a list of message and state events for a room.

        It uses pagination query parameters to paginate history in the room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomMessagesResponse` if the request was successful or
        a `RoomMessagesResponse` if there was an error with the request.

        Args:
            room_id (str): The room id of the room for which we would like to
                fetch the messages.
            start (str): The token to start returning events from. This token
                can be obtained from a prev_batch token returned for each room
                by the sync API, or from a start or end token returned by a
                previous request to this endpoint.
            end (str, optional): The token to stop returning events at. This
                token can be obtained from a prev_batch token returned for
                each room by the sync endpoint, or from a start or end token
                returned by a previous request to this endpoint.
            direction (MessageDirection, optional): The direction to return
                events from. Defaults to MessageDirection.back.
            limit (int, optional): The maximum number of events to return.
                Defaults to 10.
            message_filter (Optional[Dict[Any, Any]]):
                A filter dict that should be used for this room messages
                request.

        Example:
            >>> response = await client.room_messages(room_id, previous_batch)
            >>> next_response = await client.room_messages(room_id,
            ...                                            response.end)


        """
        method, path = self._build_request(Api.room_messages(
            self.access_token,
            room_id,
            start,
            end=end,
            direction=direction,
            limit=limit,
            message_filter=message_filter,
        ))

        label = "/_matrix/client/v3/rooms/_/messages"
        return self._send(RoomMessagesResponse, method, path, None, label, (room_id,))

    @logged_in
    def room_typing(
        self,
        room_id: str,
        typing_state: bool = True,
        timeout: int = 30000,
    ) -> Union[RoomTypingResponse, RoomTypingError]:
        """Send a typing notice to the server.

        This tells the server that the user is typing for the next N
        milliseconds or that the user has stopped typing.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomTypingResponse` if the request was successful or
        a `RoomTypingError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room where the user is typing.
            typing_state (bool): A flag representing whether the user started
                or stopped typing.
            timeout (int): For how long should the new typing notice be
                valid for in milliseconds.
        """
        method, path, data = self._build_request(Api.room_typing(
            self.access_token, room_id, self.user_id, typing_state, timeout
        ))
        label = "/_matrix/client/v3/rooms/_/typing/_"
        return self._send(RoomTypingResponse, method, path, data, label, (room_id,))

    @logged_in
    def room_get_tags(
        self,
        room_id: str,
    ) -> Tuple[RoomGetTagsResponse, RoomGetTagsError]:

        method, path, data = self._build_request(ApiExt.get_tags(
            self.access_token, self.user_id, room_id
        ))

        label = "/_matrix/client/v3/user/_/rooms/_/tags"
        return self._send(RoomGetTagsResponse, method, path, data, label)

    @logged_in
    def room_set_tags(
        self,
        room_id: str,
        tag: str,
        order: float = None,
    ) -> Tuple[RoomSetTagsResponse, RoomSetTagsError]:

        method, path, data = self._build_request(ApiExt.set_tags(
            self.access_token, self.user_id, room_id, tag, order
        ))

        label = "/_matrix/client/v3/user/_/rooms/_/tags"
        return self._send(RoomSetTagsResponse, method, path, data, label)


    @logged_in
    def update_receipt_marker(
        self,
        room_id: str,
        event_id: str,
        receipt_type: str = "m.read",
    ) -> None:
        """Update the marker of given the `receipt_type` to specified `event_id`.

        Calls receive_response() to update the client state if necessary.

        Returns either a `UpdateReceiptMarkerResponse` if the request was
        successful or a `UpdateReceiptMarkerError` if there was an error with
        the request.

        Args:
            room_id (str): Room id of the room where the marker should
                be updated
            event_id (str): The event ID the read marker should be located at
            receipt_type (str): The type of receipt to send. Currently, only
                `m.read` is supported by the Matrix specification.
        """
        method, path = self._build_request(Api.update_receipt_marker(
            self.access_token,
            room_id,
            event_id,
            receipt_type,
        ))

        label = "/_matrix/client/v3/rooms/_/receipt/m.read/_"
        return self._send(UpdateReceiptMarkerResponse, method, path, "{}", label)

    def get_displayname(
        self, user_id: Optional[str] = None
    ) -> Union[ProfileGetDisplayNameResponse, ProfileGetDisplayNameError]:
        """Get a user's display name.

        This queries the display name of a user from the server.
        The currently logged in user is queried if no user is specified.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileGetDisplayNameResponse` if the request was
        successful or a `ProfileGetDisplayNameError` if there was an error
        with the request.

        Args:
            user_id (str): User id of the user to get the display name for.
        """
        method, path = self._build_request(Api.profile_get_displayname(
            user_id or self.user_id, access_token=self.access_token or None
        ))

        label = "/_matrix/client/v3/profile/_/displayname"
        return self._send(ProfileGetDisplayNameResponse, method, path, None, label)

    @logged_in
    def set_displayname(self,
                        displayname: str
    ) -> Union[ProfileSetDisplayNameResponse, ProfileSetDisplayNameError]:
        """Set user's display name.

        This tells the server to set display name of the currently logged
        in user to the supplied string.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileSetDisplayNameResponse` if the request was
        successful or a `ProfileSetDisplayNameError` if there was an error
        with the request.

        Args:
            displayname (str): Display name to set.
        """
        method, path, data = self._build_request(Api.profile_set_displayname(
            self.access_token, self.user_id, displayname
        ))

        label = "/_matrix/client/v3/profile/_/displayname"
        return self._send(ProfileSetDisplayNameResponse, method, path, data, label)

    def get_avatar(
        self, user_id: Optional[str] = None
    ) -> Union[ProfileGetAvatarResponse, ProfileGetAvatarError]:
        """Get a user's avatar URL.

        This queries the avatar matrix content URI of a user from the server.
        The currently logged in user is queried if no user is specified.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileGetAvatarResponse` if the request was
        successful or a `ProfileGetAvatarError` if there was an error
        with the request.

        Args:
            user_id (str): User id of the user to get the avatar for.
        """
        method, path = self._build_request(Api.profile_get_avatar(
            user_id or self.user_id, access_token=self.access_token or None
        ))

        label = "/_matrix/client/v3/profile/_/avatar_url"
        return self._send(ProfileGetAvatarResponse, method, path, None, label)

    @logged_in
    def set_avatar(
        self, avatar_url: str
    ) -> Union[ProfileSetAvatarResponse, ProfileSetAvatarError]:
        """Set the user's avatar URL.

        This tells the server to set the avatar of the currently logged
        in user to supplied matrix content URI.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileSetAvatarResponse` if the request was
        successful or a `ProfileSetAvatarError` if there was an error
        with the request.

        Args:
            avatar_url (str): matrix content URI of the avatar to set.
        """
        method, path, data = self._build_request(Api.profile_set_avatar(
            self.access_token, self.user_id, avatar_url
        ))

        label = "/_matrix/client/v3/profile/_/avatar_url"
        return self._send(ProfileSetAvatarResponse, method, path, data, label)

    @logged_in
    def sync(
        self,
        timeout: Optional[int] = 0,
        sync_filter: _FilterT = None,
        since: Optional[str] = None,
        full_state: Optional[bool] = None,
        set_presence: Optional[str] = None,
    ) -> Union[SyncResponse, SyncError]:
    # tbd update docstr (also decide on _filterT???)
        """Synchronize the client's state with the latest state on the server.

        In general you should use sync_forever() which handles additional
        tasks automatically (like sending encryption keys among others).

        Calls receive_response() to update the client state if necessary.

        Args:
            timeout(int, optional): The maximum time that the server should
                wait for new events before it should return the request
                anyways, in milliseconds.
                If ``0``, no timeout is applied.
                If ``None``, use ``AsyncClient.config.request_timeout``.
                If a timeout is applied and the server fails to return after
                15 seconds of expected timeout,
                the client will timeout by itself.
            sync_filter (Union[None, str, Dict[Any, Any]):
                A filter ID that can be obtained from
                ``AsyncClient.upload_filter()`` (preferred),
                or filter dict that should be used for this sync request.
            full_state (bool, optional): Controls whether to include the full
                state for all rooms the user is a member of. If this is set to
                true, then all state events will be returned, even if since is
                non-empty. The timeline will still be limited by the since
                parameter.
            since (str, optional): A token specifying a point in time where to
                continue the sync from. Defaults to the last sync token we
                received from the server using this API call.
            set_presence (str, optional): The presence state.
                One of: ["online", "offline", "unavailable"]

        Returns either a `SyncResponse` if the request was successful or
        a `SyncError` if there was an error with the request.
        """

        sync_token = since or self.next_batch
        presence = set_presence #or self._presence
        method, path = self._build_request(Api.sync(
            self.access_token,
            since=sync_token or self.loaded_sync_token,
            timeout=timeout or None,
            filter=sync_filter,
            full_state=full_state,
            set_presence=presence,
        ))

        # response = await self._send(
        #     SyncResponse,
        #     method,
        #     path,
        #     # 0 if full_state: server doesn't respect timeout if full_state
        #     # + 15: give server a chance to naturally return before we timeout
        #     timeout=0 if full_state else timeout / 1000 + 15 if timeout else timeout,
        # )
        label = "/_matrix/client/v3/sync"
        return self._send(SyncResponse, method, path, name=label)
