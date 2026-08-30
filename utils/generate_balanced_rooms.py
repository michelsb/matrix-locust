#!/usr/bin/env python3

import argparse
import csv
import json
import random

PARETO_ALPHA = 1.161


def load_users(csv_path):
    users = []
    with open(csv_path, "r", encoding="utf-8", newline="") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            username = row["username"].strip()
            if username:
                users.append(username)
    return users


def make_even_room_size(raw_size, max_room_size):
    size = max(2, min(raw_size, max_room_size))
    if size % 2 != 0:
        if size < max_room_size:
            size += 1
        else:
            size -= 1
    return size


def generate_room_sizes(max_num_rooms, max_room_size):
    room_sizes = []
    for _ in range(max_num_rooms):
        raw_size = round(random.paretovariate(PARETO_ALPHA))
        size = make_even_room_size(raw_size, max_room_size)
        if size >= 2:
            room_sizes.append(size)
    return room_sizes


def normalize_target_homeserver(target_homeserver):
    if target_homeserver in ("home01", "01"):
        return "home01"
    if target_homeserver in ("home02", "02"):
        return "home02"
    raise ValueError("target_homeserver must be one of: home01, home02, 01, 02")


def analyze_assignments(users, room_members, label):
    assignments = {}
    for members in room_members.values():
        for member in members:
            assignments[member] = assignments.get(member, 0) + 1

    roomless = [user for user in users if assignments.get(user, 0) < 1]
    total_rooms = len(room_members)
    in_all_rooms = [user for user in users if assignments.get(user, 0) == total_rooms]
    centurions = [user for user in users if assignments.get(user, 0) > 99]

    print(f"{label}: {len(roomless)} users in zero rooms")
    print(f"{label}: {len(in_all_rooms)} users in all rooms")
    print(f"{label}: {len(centurions)} users in > 100 rooms")


def main():
    parser = argparse.ArgumentParser(
        description="Generate room assignments with 50%% of users from each homeserver in every room."
    )
    parser.add_argument("--users01", default="users01.csv", help="CSV file for homeserver 01 users.")
    parser.add_argument("--users02", default="users02.csv", help="CSV file for homeserver 02 users.")
    parser.add_argument("--output", default="rooms_balanced.json", help="Output JSON file.")
    parser.add_argument(
        "--target-homeserver",
        choices=["home01", "home02", "01", "02"],
        help="Homeserver that should own the rooms. The first user in each room will come from this homeserver.",
    )
    parser.add_argument(
        "--max-rooms",
        type=int,
        help="Maximum number of rooms to attempt to generate. Defaults to the total number of loaded users.",
    )
    parser.add_argument("--seed", type=int, help="Optional random seed for reproducible output.")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    users01 = load_users(args.users01)
    users02 = load_users(args.users02)

    if not users01 or not users02:
        raise ValueError("Both homeserver user files must contain at least one user.")

    max_room_size = min(len(users01), len(users02)) * 2
    max_num_rooms = args.max_rooms or (len(users01) + len(users02))
    room_sizes = generate_room_sizes(max_num_rooms, max_room_size)

    if not room_sizes:
        raise ValueError("No valid room sizes were generated.")

    room_members = {}
    target_homeserver = normalize_target_homeserver(args.target_homeserver) if args.target_homeserver else None
    for index, room_size in enumerate(room_sizes):
        members_per_homeserver = room_size // 2
        selected01 = random.sample(users01, members_per_homeserver)
        selected02 = random.sample(users02, members_per_homeserver)

        if target_homeserver == "home01":
            creator = selected01[0]
            remaining_members = selected01[1:] + selected02
        elif target_homeserver == "home02":
            creator = selected02[0]
            remaining_members = selected01 + selected02[1:]
        else:
            creator = None
            remaining_members = selected01 + selected02

        random.shuffle(remaining_members)
        members = [creator] + remaining_members if creator else remaining_members
        room_members[f"Room {index}"] = members

    with open(args.output, "w", encoding="utf-8") as jsonfile:
        json.dump(room_members, jsonfile, indent=2)

    room_sizes_sum = sum(room_sizes)
    print("###################################")
    print(f"{len(room_sizes)} Total rooms")
    print(f"Max = {max(room_sizes):.6f}")
    print(f"Min = {min(room_sizes):.6f}")
    print(f"Avg = {room_sizes_sum / len(room_sizes):.6f}")
    print("###################################")

    analyze_assignments(users01, room_members, "home01")
    analyze_assignments(users02, room_members, "home02")
    print(f"Saved balanced rooms to {args.output}")


if __name__ == "__main__":
    main()
