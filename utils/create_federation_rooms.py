#!/usr/bin/env python3
"""
Script to create rooms for federation testing on both homeservers.
Creates rooms from rooms01.json on home01 and rooms02.json on home02.
"""

import json
import logging
import csv
import requests
import sys

# Configuração do logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# URLs dos servidores Matrix
HOMESERVERS = {
    "home01": "https://home01-dev.ac.atlab.ufc.br",
    "home02": "https://home02-dev.ac.atlab.ufc.br"
}

# Mapping from homeserver key to the correct Matrix domain
HOMESERVER_TO_DOMAIN = {
    "home01": "home01-dev.ac.atlab.ufc.br",
    "home02": "home02-dev.ac.atlab.ufc.br",
}

# Mapping from local username prefixes to homeserver keys
USER_PREFIX_TO_HOMESERVER = {
    "userh01.": "home01",
    "userhome01.": "home01",
    "userh02.": "home02",
    "userhome02.": "home02",
}


def normalize_username(username):
    """Normalize usernames from CSV/JSON to localpart format."""
    normalized = username.strip()

    # Full Matrix IDs are accepted in inputs, but for local credential lookup/login
    # we keep only the localpart.
    if normalized.startswith("@"):
        normalized = normalized[1:]

    # Legacy datasets may have a trailing ':' in usernames (for example userh01.000:).
    if normalized.endswith(":"):
        normalized = normalized[:-1]

    # If a domain is present, keep only the localpart.
    if ":" in normalized:
        normalized = normalized.split(":", 1)[0]

    return normalized


def infer_homeserver(username):
    """Infer homeserver key from local username prefix."""
    normalized = normalize_username(username)
    for prefix, homeserver in USER_PREFIX_TO_HOMESERVER.items():
        if normalized.startswith(prefix):
            return homeserver
    raise ValueError(f"Cannot infer homeserver for username '{username}'")

def load_users(file_path):
    """Load users from CSV file."""
    users = []
    with open(file_path, mode='r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            users.append({
                'username': normalize_username(row['username']),
                'password': row['password']
            })
    return users

def authenticate_user(server_url, username, password):
    """Authenticate user and get access token."""
    url = f"{server_url}/_matrix/client/v3/login"
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
        "initial_device_display_name": "matrix-create-federation-room"
    }
    response = requests.post(url, json=payload)

    if response.status_code == 200:
        response_data = response.json()
        logging.info(f"User '{username}' authenticated successfully on {server_url}")
        return response_data['access_token']
    else:
        logging.error(f"Failed to authenticate user '{username}' on {server_url}: {response.status_code} {response.text}")
        return None

def make_matrix_user_id(username):
    """Build a full Matrix user ID from a local username."""
    username = username.strip()

    # Already a complete Matrix ID
    if username.startswith("@") and ":" in username and not username.endswith(":"):
        return username

    localpart = normalize_username(username)
    homeserver = infer_homeserver(localpart)
    domain = HOMESERVER_TO_DOMAIN[homeserver]
    return f"@{localpart}:{domain}"


def create_room(server_url, access_token, room_name, user_ids):
    """Create a room and invite users."""
    url = f"{server_url}/_matrix/client/v3/createRoom"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "name": room_name,
        "invite": user_ids,
        "preset": "private_chat",  # Allow invited users to join
        "is_direct": False
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 200:
        response_data = response.json()
        room_id = response_data.get('room_id')
        logging.info(f"Room '{room_name}' created successfully with ID {room_id}")
        return room_id
    else:
        logging.error(f"Failed to create room '{room_name}': {response.status_code}")
        logging.debug(f"Response: {response.text}")
        return None

def create_rooms_for_homeserver(homeserver_name, rooms_file, users_file):
    """Create rooms for a specific homeserver."""
    server_url = HOMESERVERS[homeserver_name]

    logging.info(f"Loading rooms from {rooms_file}")
    with open(rooms_file, "r", encoding="utf-8") as file:
        rooms = json.load(file)

    logging.info(f"Loading users from {users_file}")
    users = load_users(users_file)

    # Create a lookup dict for user credentials
    user_credentials = {user['username']: user for user in users}

    rooms_created = 0
    rooms_failed = 0

    for room_name, room_users in rooms.items():
        if not room_users:
            logging.warning(f"Room '{room_name}' has no users, skipping")
            continue

        normalized_room_users = [normalize_username(username) for username in room_users]

        # Pick a creator that belongs to the current homeserver and has credentials.
        creator_username = None
        for username in normalized_room_users:
            try:
                if infer_homeserver(username) == homeserver_name and username in user_credentials:
                    creator_username = username
                    break
            except ValueError as e:
                logging.error(f"Invalid username in room '{room_name}': {e}")

        if not creator_username:
            logging.error(
                f"No valid creator from {homeserver_name} found in room '{room_name}'"
            )
            rooms_failed += 1
            continue

        # Prepare invitees (all users except creator)
        invitees = []
        for username in normalized_room_users:
            if username == creator_username:
                continue
            try:
                invitees.append(make_matrix_user_id(username))
            except ValueError as e:
                logging.error(f"Invalid username in room '{room_name}': {e}")
                continue

        logging.info(f"Creating room '{room_name}' with {len(invitees)+1} users on {homeserver_name}")

        # Get creator credentials
        creator_creds = user_credentials.get(creator_username)
        if not creator_creds:
            logging.error(f"Credentials not found for user '{creator_username}'")
            rooms_failed += 1
            continue

        # Authenticate creator
        access_token = authenticate_user(server_url, creator_creds['username'], creator_creds['password'])
        if not access_token:
            logging.error(f"Could not authenticate creator '{creator_username}'")
            rooms_failed += 1
            continue

        # Create room
        room_id = create_room(server_url, access_token, room_name, invitees)
        if room_id:
            rooms_created += 1
        else:
            rooms_failed += 1

    logging.info(f"Finished creating rooms for {homeserver_name}: {rooms_created} created, {rooms_failed} failed")
    return rooms_created, rooms_failed

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Create federation rooms on one or both homeservers.")
    parser.add_argument("homeserver", nargs="?", choices=list(HOMESERVERS.keys()),
                        help="Homeserver to create rooms on: home01 or home02")
    parser.add_argument("--rooms-file", help="Path to the JSON file containing room definitions.")
    parser.add_argument("--users-file", help="Path to the CSV file containing user credentials.")
    args = parser.parse_args()

    if args.homeserver:
        rooms_file = args.rooms_file or f"rooms{args.homeserver[-2:]}.json"
        users_file = args.users_file or f"users{args.homeserver[-2:]}.csv"
        create_rooms_for_homeserver(args.homeserver, rooms_file, users_file)
    else:
        logging.info("Creating rooms for both homeservers...")

        results = {}
        for homeserver in HOMESERVERS.keys():
            rooms_file = args.rooms_file or f"rooms{homeserver[-2:]}.json"
            users_file = args.users_file or f"users{homeserver[-2:]}.csv"

            created, failed = create_rooms_for_homeserver(homeserver, rooms_file, users_file)
            results[homeserver] = {"created": created, "failed": failed}

        # Summary
        total_created = sum(r["created"] for r in results.values())
        total_failed = sum(r["failed"] for r in results.values())

        logging.info("=== SUMMARY ===")
        for homeserver, stats in results.items():
            logging.info(f"{homeserver}: {stats['created']} created, {stats['failed']} failed")
        logging.info(f"Total: {total_created} rooms created, {total_failed} failed")

if __name__ == "__main__":
    main()