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

# temp containing extensions for future PRs

from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass

from nio.api import Api
from nio import ErrorResponse, Response, Schemas
from nio.responses import verify


class ApiExt(Api):
    @staticmethod
    def get_tags(
        access_token: str,
        user_id: str,
        room_id: str,
    ) -> Tuple[str, str]:
        """Update the marker of given `receipt_type` to specified `event_id`.

        Returns the HTTP method and HTTP path for the request.

        Args:
            access_token (str): The access token to be used with the request.
            room_id (str): Room id of the room where the marker should
                be updated
            event_id (str): The event ID the read marker should be located at
            receipt_type (str): The type of receipt to send. Currently, only
                `m.read` is supported by the Matrix specification.
        """
        query_parameters = {"access_token": access_token}
        path = ["user", user_id, "rooms", room_id, "tags"]

        #  GET /_matrix/client/v3/user/{userId}/rooms/{roomId}/tags
        # body = {
        #     "visibility": visibility.value,
        #     "creation_content": {"m.federate": federate},
        #     "is_direct": is_direct,
        # }

        # if alias:
        #     body["room_alias_name"] = alias

        # if name:
        #     body["name"] = name


        return ("GET", Api._build_path(path, query_parameters))
        # return ("POST", Api._build_path(path, query_parameters), Api.to_json(body))

    @staticmethod
    def set_tags(
        access_token: str,
        user_id: str,
        room_id: str,
        tag: str,
        order: float = None,
    ) -> Tuple[str, str]:
        """Update the marker of given `receipt_type` to specified `event_id`.

        Returns the HTTP method and HTTP path for the request.

        Args:
            access_token (str): The access token to be used with the request.
            room_id (str): Room id of the room where the marker should
                be updated
            event_id (str): The event ID the read marker should be located at
            receipt_type (str): The type of receipt to send. Currently, only
                `m.read` is supported by the Matrix specification.
        """
        query_parameters = {"access_token": access_token}
        path = ["user", user_id, "rooms", room_id, "tags", tag]

        #  PUT /_matrix/client/v3/user/{userId}/rooms/{roomId}/tags/{tag}
        body = {}

        if not(order is None):
            body["order"] = order

        # add validation for the following?

        #order: A number in a range [0,1] describing a relative position of the room under the given tag.

        # if alias:
        #     body["room_alias_name"] = alias

        # if name:
        #     body["name"] = name


        # return ("PUT", Api._build_path(path, query_parameters))
        return ("PUT", Api._build_path(path, query_parameters), Api.to_json(body))


# todo: add remove tags (DELETE)

class RoomGetTagsError(ErrorResponse):
    pass

class RoomSetTagsError(ErrorResponse):
    pass


@dataclass
class RoomGetTagsResponse(Response):
    """Response representing a successful get avatar request.

    Attributes:
        avatar_url (str, optional): The matrix content URI for the user's
            avatar. None if the user doesn't have an avatar.
    """

    tags: Optional[str] = None

    def __str__(self) -> str:
        return f"Tags: {self.tags}"

    @classmethod
    @verify(Schemas.tags, RoomGetTagsError)
    def from_dict(
        cls,
        parsed_dict: (Dict[Any, Any]),
    ):
        # type: (...) -> Union[ProfileGetAvatarResponse, ErrorResponse]
        return cls(parsed_dict.get("tags"))

@dataclass
class RoomSetTagsResponse(Response):
    """Response representing a successful get avatar request.

    Attributes:
        avatar_url (str, optional): The matrix content URI for the user's
            avatar. None if the user doesn't have an avatar.
    """

    tags: Optional[str] = None

    def __str__(self) -> str:
        return f"Tags: {self.tags}"

    @classmethod
    @verify(Schemas.tags, RoomSetTagsError)
    def from_dict(
        cls,
        parsed_dict: (Dict[Any, Any]),
    ):
        # type: (...) -> Union[ProfileGetAvatarResponse, ErrorResponse]
        return cls(parsed_dict.get("tags"))


