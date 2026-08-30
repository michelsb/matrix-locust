from matrix_locust.users.matrixchatuser import MatrixChatUser


def test_media_upload_404_is_treated_as_unsupported():
    assert MatrixChatUser.should_fallback_to_text_upload(
        404,
        {"errcode": "M_UNRECOGNIZED", "error": "Unrecognized request"},
    )


def test_media_upload_valid_response_is_not_fallback():
    assert not MatrixChatUser.should_fallback_to_text_upload(
        200,
        {"content_uri": "mxc://example.org/abc"},
    )
