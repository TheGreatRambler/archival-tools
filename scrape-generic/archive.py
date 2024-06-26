import os
import sys
import anyio
import requests
from dotenv import load_dotenv
from nintendo.nex import (
    backend,
    ranking,
    datastore,
    settings,
    prudp,
    authentication,
    rmc,
    common,
)
from nintendo.nex.common import RMCError
from nintendo import nnas, nasc
from nintendo.nnas import NexToken
from anynet import http
import hashlib
import hmac
import struct
import threading
import time
import sqlite3
from multiprocessing import Process, Lock, Queue, Array, Value
import multiprocessing
import json
import queue
import traceback
import asyncio
import gzip
import httpx

import logging

logging.basicConfig(level=logging.FATAL)

load_dotenv()

DEVICE_ID = int(os.getenv("DEVICE_ID"))
SERIAL_NUMBER = os.getenv("SERIAL_NUMBER")
SYSTEM_VERSION = int(os.getenv("SYSTEM_VERSION"), 16)
REGION_ID = int(os.getenv("REGION_ID"))
COUNTRY_NAME = os.getenv("COUNTRY")
LANGUAGE = os.getenv("LANGUAGE")

USERNAME = os.getenv("NEX_USERNAME")
PASSWORD = os.getenv("NEX_PASSWORD")

SERIAL_NUMBER_3DS = os.getenv("3DS_SERIAL_NUMBER")
MAC_ADDRESS_3DS = os.getenv("3DS_MAC_ADDRESS")
FCD_CERT_3DS = bytes.fromhex(os.getenv("3DS_FCD_CERT"))
USERNAME_3DS = int(os.getenv("3DS_USERNAME"))
USERNAME_HMAC_3DS = os.getenv("3DS_USERNAME_HMAC")
PID_3DS = os.getenv("3DS_PID")
PASSWORD_3DS = os.getenv("3DS_PASSWORD")

REGION_3DS = int(os.getenv("3DS_REGION"))
LANGUAGE_3DS = int(os.getenv("3DS_LANG"))

ORDINAL_RANKING = 1  # 1234 rather than 1224

RANKING_DB = "3ds_ranking_first_batch.db"
RANKING_LOG = "3ds_ranking_first_batch_log.txt"

if "datastore" in sys.argv[1]:
    DATASTORE_DB = "%s.db" % sys.argv[2]
    DATASTORE_LOG = "%s_log.txt" % sys.argv[2]


async def retry_if_rmc_error(func, s, host, port, pid, password, auth_info=None):
    try:
        async with backend.connect(s, host, port) as be:
            try:
                async with be.login(pid, password, auth_info) as client:
                    return await func(client)
            except RuntimeError as e:
                print('"PRUDP connection failed" encountered: ', e)
                # Reattempt until success recursively
                return await retry_if_rmc_error(func, s, host, port, pid, password)
    except RuntimeError as e:
        print('"RMC connection is closed" encountered: ', e)
        # Reattempt until success recursively
        return await retry_if_rmc_error(func, s, host, port, pid, password)


# Category testing thread
def range_test_category(
    access_key,
    nex_version,
    host,
    port,
    pid,
    password,
    start,
    end,
    found_queue,
    num_tested_queue,
):
    async def run():
        s = settings.default()
        s.configure(access_key, nex_version)

        try:
            async with backend.connect(s, host, port) as be:
                async with be.login(pid, password) as client:
                    ranking_client = ranking.RankingClient(client)

                    num_tested = 0
                    for category in range(start, end):
                        try:
                            order_param = ranking.RankingOrderParam()
                            order_param.offset = 0
                            order_param.count = 1

                            _ = await ranking_client.get_ranking(
                                ranking.RankingMode.GLOBAL,  # Get the global leaderboard
                                category,  # Category, this is 3-A (Magrove Cove)
                                order_param,
                                0,
                                0,
                            )

                            # No exception, this is a valid category
                            found_queue.put(category)
                        except Exception:
                            None

                        num_tested += 1

                        if num_tested % 100 == 0:
                            num_tested_queue.put(100)
        except Exception as e:
            print(e)

        found_queue.put(-1)

    anyio.run(run)


def print_categories(num_processes, found_queue, num_tested_queue):
    begin = time.perf_counter()
    num_tested = 0
    num_sentinels = 0
    while True:
        time.sleep(0.1)

        try:
            category = found_queue.get(block=False)

            # Use sentinels
            if category == -1:
                num_sentinels += 1
                if num_sentinels == num_processes:
                    print("Ending print process")
                    break

            print("Found category %d" % category)
        except queue.Empty:
            None

        try:
            num_tested_add = num_tested_queue.get(block=False)
            num_tested += num_tested_add

            cur = time.perf_counter()
            print(
                "Tested %d in %f seconds, would take %d seconds or %d days"
                % (
                    num_tested,
                    cur - begin,
                    (cur - begin) / num_tested * pow(2, 32),
                    ((cur - begin) / num_tested * pow(2, 32)) / 86400,
                )
            )
        except queue.Empty:
            None


def run_category_scrape(
    category,
    log_lock,
    s,
    host,
    port,
    pid,
    password,
    game,
    pretty_game_id,
    has_datastore,
    i,
    nex_wiiu_games,
    auth_info=None,
):
    async def main():
        con = sqlite3.connect(RANKING_DB, timeout=3600)
        print("Starting category %d" % category)

        last_rank_seen = 0
        num_ranks_seen = 0
        last_pid_seen = None
        last_id_seen = None
        rankings = None

        # One request to get first PID and number of rankings, just in case offset based fails on first request
        try:

            async def get_start_data(client):
                ranking_client = ranking.RankingClient(client)

                order_param = ranking.RankingOrderParam()
                order_param.offset = 0
                order_param.count = 1

                rankings = await ranking_client.get_ranking(
                    ranking.RankingMode.GLOBAL,  # Get the global leaderboard
                    category,
                    order_param,
                    0,
                    0,
                )

                return (rankings, rankings.data[0].pid, rankings.data[0].unique_id)

            rankings, last_pid_seen, last_id_seen = await retry_if_rmc_error(
                get_start_data, s, host, port, str(pid), password
            )
        except Exception as e:
            # Protocol is likely incorrect
            log_lock.acquire()
            log_file = open(RANKING_LOG, "a", encoding="utf-8")
            print_and_log(
                "Have %d and issue with %s at category %d: %s"
                % (
                    num_ranks_seen,
                    game["name"].replace("\n", " "),
                    category,
                    "".join(traceback.TracebackException.from_exception(e).format()),
                ),
                log_file,
            )
            log_file.close()
            log_lock.release()
            return

        # Get number of rankings with this category
        num_ranks_seen = list(
            con.execute(
                "SELECT COUNT(*) FROM ranking WHERE game = ? AND category = ?",
                (pretty_game_id, category),
            )
        )[0][0]

        offset_interval = 255
        if num_ranks_seen >= rankings.total:
            log_lock.acquire()
            log_file = open(RANKING_LOG, "a", encoding="utf-8")
            print_and_log("Stopping category %d, already finished" % category, log_file)
            log_file.close()
            log_lock.release()
        elif num_ranks_seen == 0:
            # Try offset
            cur_offset = 0
            finish_after_this_one = False
            while True:
                try:

                    async def get_rankings(client):
                        ranking_client = ranking.RankingClient(client)

                        order_param = ranking.RankingOrderParam()
                        order_param.order_calc = ORDINAL_RANKING
                        order_param.offset = cur_offset
                        order_param.count = offset_interval

                        rankings = await ranking_client.get_ranking(
                            ranking.RankingMode.GLOBAL,  # Get the global leaderboard
                            category,
                            order_param,
                            0,
                            0,
                        )

                        return rankings

                    rankings = await retry_if_rmc_error(
                        get_rankings,
                        s,
                        host,
                        port,
                        str(pid),
                        password,
                        auth_info=auth_info,
                    )

                    await add_rankings(
                        category,
                        s,
                        host,
                        port,
                        pid,
                        password,
                        log_lock,
                        rankings,
                        pretty_game_id,
                        has_datastore,
                        con,
                        auth_info=auth_info,
                    )

                    last_rank_seen = rankings.data[-1].rank
                    last_pid_seen = rankings.data[-1].pid
                    last_id_seen = rankings.data[-1].unique_id
                    num_ranks_seen += len(rankings.data)

                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d out of %d for category %d for %s (%d out of %d)"
                        % (
                            num_ranks_seen,
                            rankings.total,
                            category,
                            game["name"].replace("\n", " "),
                            i,
                            len(nex_wiiu_games),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()

                    if finish_after_this_one:
                        break

                    cur_offset += len(rankings.data)
                except RMCError as e:
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d and RMCError with %s at category %d: %s"
                        % (
                            num_ranks_seen,
                            game["name"].replace("\n", " "),
                            category,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                    break
                except Exception as e:
                    # Protocol is likely incorrect
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d and issue with %s at category %d: %s"
                        % (
                            num_ranks_seen,
                            game["name"].replace("\n", " "),
                            category,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                    break

            # For games that limit to 1000 try mode = 1 approach (around specific player)
            while True:
                try:

                    async def get_rankings(client):
                        ranking_client = ranking.RankingClient(client)

                        order_param = ranking.RankingOrderParam()
                        order_param.order_calc = ORDINAL_RANKING
                        order_param.offset = 0
                        order_param.count = offset_interval

                        rankings = await ranking_client.get_ranking(
                            ranking.RankingMode.GLOBAL_AROUND_SELF,  # Get the leaderboard around this player
                            category,
                            order_param,
                            last_id_seen,
                            last_pid_seen,
                        )

                        return rankings

                    rankings = await retry_if_rmc_error(
                        get_rankings,
                        s,
                        host,
                        port,
                        str(pid),
                        password,
                        auth_info=auth_info,
                    )

                    rankings.data = list(
                        filter(lambda entry: entry.rank > last_rank_seen, rankings.data)
                    )

                    # If none of the players around this player are unique assume done for now
                    if len(rankings.data) == 0:
                        break

                    await add_rankings(
                        category,
                        s,
                        host,
                        port,
                        pid,
                        password,
                        log_lock,
                        rankings,
                        pretty_game_id,
                        has_datastore,
                        con,
                        auth_info=auth_info,
                    )

                    last_rank_seen = rankings.data[-1].rank
                    last_pid_seen = rankings.data[-1].pid
                    last_id_seen = rankings.data[-1].unique_id
                    num_ranks_seen += len(rankings.data)

                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d out of %d for category %d for %s (%d out of %d)"
                        % (
                            num_ranks_seen,
                            rankings.total,
                            category,
                            game["name"].replace("\n", " "),
                            i,
                            len(nex_wiiu_games),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                except RMCError as e:
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d and RMCError with %s at category %d: %s"
                        % (
                            num_ranks_seen,
                            game["name"].replace("\n", " "),
                            category,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                    break
                except Exception as e:
                    # Protocol is likely incorrect
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d and issue with %s at category %d: %s"
                        % (
                            num_ranks_seen,
                            game["name"].replace("\n", " "),
                            category,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                    break
        elif num_ranks_seen < rankings.total:
            # Get last id and pid seen
            result = list(
                con.execute(
                    "SELECT rank, id, pid FROM ranking WHERE game = ? AND category = ? ORDER BY rank DESC LIMIT 1",
                    (pretty_game_id, category),
                )
            )[0]
            last_rank_seen = int(result[0])
            last_id_seen = int(result[1])
            last_pid_seen = int(result[2])

            # Only use around_self for this
            while True:
                try:

                    async def get_rankings(client):
                        ranking_client = ranking.RankingClient(client)

                        order_param = ranking.RankingOrderParam()
                        order_param.order_calc = ORDINAL_RANKING
                        order_param.offset = 0
                        order_param.count = offset_interval

                        rankings = await ranking_client.get_ranking(
                            ranking.RankingMode.GLOBAL_AROUND_SELF,  # Get the leaderboard around this player
                            category,
                            order_param,
                            last_id_seen,
                            last_pid_seen,
                        )

                        return rankings

                    rankings = await retry_if_rmc_error(
                        get_rankings,
                        s,
                        host,
                        port,
                        str(pid),
                        password,
                        auth_info=auth_info,
                    )

                    rankings.data = list(
                        filter(lambda entry: entry.rank > last_rank_seen, rankings.data)
                    )

                    # If none of the players around this player are unique assume done for now
                    if len(rankings.data) == 0:
                        break

                    await add_rankings(
                        category,
                        s,
                        host,
                        port,
                        pid,
                        password,
                        log_lock,
                        rankings,
                        pretty_game_id,
                        has_datastore,
                        con,
                    )

                    last_rank_seen = rankings.data[-1].rank
                    last_pid_seen = rankings.data[-1].pid
                    last_id_seen = rankings.data[-1].unique_id
                    num_ranks_seen += len(rankings.data)

                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d out of %d for category %d for %s (%d out of %d)"
                        % (
                            num_ranks_seen,
                            rankings.total,
                            category,
                            game["name"].replace("\n", " "),
                            i,
                            len(nex_wiiu_games),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                except RMCError as e:
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d and RMCError with %s at category %d: %s"
                        % (
                            num_ranks_seen,
                            game["name"].replace("\n", " "),
                            category,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                    break
                except Exception as e:
                    # Protocol is likely incorrect
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Have %d and issue with %s at category %d: %s"
                        % (
                            num_ranks_seen,
                            game["name"].replace("\n", " "),
                            category,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()
                    break

        con.close()

    anyio.run(main)


def get_datastore_data(
    log_lock,
    access_key,
    nex_version,
    host,
    port,
    pid,
    password,
    pretty_game_id,
    metas_queue,
    done_flag,
    auth_info=None,
):
    async def run():
        s = settings.default()
        s.configure(access_key, nex_version)

        con = sqlite3.connect(DATASTORE_DB, timeout=3600)

        try:

            while True:
                time.sleep(0.5)

                try:
                    entries = metas_queue.get(block=False)

                    log_lock.acquire()
                    log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Start download of %d entries" % len(entries), log_file
                    )
                    log_file.close()
                    log_lock.release()

                    for entry in entries:
                        try:
                            data_id, owner_id = entry

                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log("Start %d" % data_id, log_file)
                            log_file.close()
                            log_lock.release()

                            start = time.perf_counter()

                            async def get_req_info(client):
                                store = datastore.DataStoreClient(client)

                                get_param = datastore.DataStorePrepareGetParam()
                                get_param.data_id = data_id

                                req_info = await store.prepare_get_object(get_param)
                                headers = {
                                    header.key: header.value
                                    for header in req_info.headers
                                }

                                return (req_info.url, req_info, headers)

                            url, req_info, headers = await retry_if_rmc_error(
                                get_req_info,
                                s,
                                host,
                                port,
                                str(pid),
                                password,
                                auth_info=auth_info,
                            )

                            async with httpx.AsyncClient() as client:
                                response = await client.get(
                                    "https://%s" % req_info.url,
                                    headers=headers,
                                    timeout=(60 * 10),
                                )

                            # TODO store the headers too
                            con.execute(
                                "INSERT INTO datastore_data (game, data_id, url, data) values (?, ?, ?, ?)",
                                (
                                    pretty_game_id,
                                    data_id,
                                    url,
                                    gzip.compress(response.content),
                                ),
                            )
                            con.commit()

                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log(
                                "Downloaded %d in %f seconds"
                                % (data_id, time.perf_counter() - start),
                                log_file,
                            )
                            log_file.close()
                            log_lock.release()

                        except RMCError as e:
                            print(e)
                            con.execute(
                                "INSERT INTO datastore_data (game, data_id, error) values (?, ?, ?)",
                                (pretty_game_id, data_id, str(e)),
                            )
                            con.commit()
                        except httpx.TimeoutException as e:
                            print(e)
                            con.execute(
                                "INSERT INTO datastore_data (game, data_id, error) values (?, ?, ?)",
                                (pretty_game_id, data_id, str(e)),
                            )
                            con.commit()
                except queue.Empty:
                    if bool(done_flag.value):
                        break
        except Exception as e:
            print(e)

        con.close()

    anyio.run(run)

def get_datastore_data_and_metas(
    log_lock,
    access_key,
    nex_version,
    host,
    port,
    pid,
    password,
    pretty_game_id,
    metas_queue,
    done_flag,
    s,
    auth_info=None,
):
    async def run():
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)

        try:

            while True:
                time.sleep(0.5)

                can_download_metas = True
                can_download_objects = True

                try:
                    entries = metas_queue.get(block=False)

                    log_lock.acquire()
                    log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Start download of %d entries" % len(entries), log_file
                    )
                    log_file.close()
                    log_lock.release()

                    download_entries = None
                    try:
                        async def get_res(client):
                            store = datastore.DataStoreClient(client)

                            param = datastore.DataStoreGetMetaParam()
                            param.result_option = 0xFF
                            res = await store.get_metas([x[0] for x in entries], param)

                            return res

                        res = await retry_if_rmc_error(
                            get_res, s, host, port, str(pid), password, auth_info=auth_info
                        )

                        # Remove invalid
                        meta_entries = [
                            entry
                            for i, entry in enumerate(res.info)
                            if res.results[i].is_success()
                        ]

                        con.executemany(
                            "INSERT INTO datastore_meta (game, data_id, owner_id, size, name, data_type, meta_binary, permission, delete_permission, create_time, update_time, period, status, referred_count, refer_data_id, flag, referred_time, expire_time) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    pretty_game_id,
                                    entry.data_id,
                                    str(entry.owner_id),
                                    entry.size,
                                    entry.name,
                                    entry.data_type,
                                    entry.meta_binary,
                                    entry.permission.permission,
                                    entry.delete_permission.permission,
                                    timestamp_if_not_null(entry.create_time),
                                    timestamp_if_not_null(entry.update_time),
                                    entry.period,
                                    entry.status,
                                    entry.referred_count,
                                    entry.refer_data_id,
                                    entry.flag,
                                    timestamp_if_not_null(entry.referred_time),
                                    timestamp_if_not_null(entry.expire_time),
                                )
                                for entry in meta_entries
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_meta_tag (game, data_id, tag) values (?, ?, ?)",
                            [
                                (pretty_game_id, entry.data_id, tag)
                                for entry in meta_entries
                                for tag in entry.tags
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_meta_rating (game, data_id, slot, total_value, count, initial_value) values (?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    pretty_game_id,
                                    entry.data_id,
                                    rating.slot,
                                    rating.info.total_value,
                                    rating.info.count,
                                    rating.info.initial_value,
                                )
                                for entry in meta_entries
                                for rating in entry.ratings
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_permission_recipients (game, data_id, is_delete, recipient) values (?, ?, ?, ?)",
                            [
                                (pretty_game_id, entry.data_id, 0, str(recipient))
                                for entry in meta_entries
                                for recipient in entry.permission.recipients
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_permission_recipients (game, data_id, is_delete, recipient) values (?, ?, ?, ?)",
                            [
                                (pretty_game_id, entry.data_id, 1, str(recipient))
                                for entry in meta_entries
                                for recipient in entry.delete_permission.recipients
                            ],
                        )
                        con.commit()

                        download_entries = [(entry.data_id, 0) for entry in meta_entries if entry.size > 0]
                    except RMCError as e:
                        log_lock.acquire()
                        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                        print_and_log("This game doesn't seem to support get_metas: %s" % str(e), log_file)
                        log_file.close()
                        log_lock.release()

                        download_entries = entries
                        can_download_metas = False

                    if len(download_entries) == 0:
                        can_download_metas = False
                        can_download_objects = False

                    for entry in download_entries:
                        try:
                            data_id, owner_id = entry

                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log("Start %d" % data_id, log_file)
                            log_file.close()
                            log_lock.release()

                            start = time.perf_counter()

                            async def get_req_info(client):
                                store = datastore.DataStoreClient(client)

                                get_param = datastore.DataStorePrepareGetParam()
                                get_param.data_id = data_id

                                req_info = await store.prepare_get_object(get_param)
                                headers = {
                                    header.key: header.value
                                    for header in req_info.headers
                                }

                                return (req_info.url, req_info, headers)

                            url, req_info, headers = await retry_if_rmc_error(
                                get_req_info,
                                s,
                                host,
                                port,
                                str(pid),
                                password,
                                auth_info=auth_info,
                            )

                            async with httpx.AsyncClient() as client:
                                response = await client.get(
                                    "https://%s" % req_info.url,
                                    headers=headers,
                                    timeout=(60 * 10),
                                )

                            # TODO store the headers too
                            con.execute(
                                "INSERT INTO datastore_data (game, data_id, url, data) values (?, ?, ?, ?)",
                                (
                                    pretty_game_id,
                                    data_id,
                                    url,
                                    gzip.compress(response.content),
                                ),
                            )
                            con.commit()

                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log(
                                "Downloaded %d in %f seconds"
                                % (data_id, time.perf_counter() - start),
                                log_file,
                            )
                            log_file.close()
                            log_lock.release()

                        except RMCError as e:
                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log("This game doesn't seem to support prepare_get_object: %s" % str(e), log_file)
                            log_file.close()
                            log_lock.release()

                            can_download_objects = False
                            break
                except queue.Empty:
                    if bool(done_flag.value):
                        break

                if not can_download_metas and not can_download_objects:
                    break
        except Exception as e:
            print(e)

        con.close()

    anyio.run(run)


def get_datastore_metas(
    log_lock,
    access_key,
    nex_version,
    host,
    port,
    pid,
    password,
    pretty_game_id,
    metas_queue,
    done_flag,
    process_index,
    total_num_processes,
    max_queryable,
    last_data_id,
    late_data_id,
    num_metas_threads_done,
    auth_info=None,
):
    async def run():
        try:
            s = settings.default()
            s.configure(access_key, nex_version)

            con = sqlite3.connect(DATASTORE_DB, timeout=3600)

            # Start at offset
            nonlocal last_data_id
            last_data_id += process_index * max_queryable
            have_seen_late_data_id = False

            while True:
                log_lock.acquire()
                log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                print_and_log("Starting at %d" % last_data_id, log_file)
                log_file.close()
                log_lock.release()

                async def get_res(client):
                    store = datastore.DataStoreClient(client)

                    param = datastore.DataStoreGetMetaParam()
                    param.result_option = 0xFF
                    res = await store.get_metas(
                        list(range(last_data_id, last_data_id + max_queryable)), param
                    )

                    return res

                res = await retry_if_rmc_error(
                    get_res, s, host, port, str(pid), password, auth_info=auth_info
                )

                # Remove invalid
                entries = [
                    entry
                    for i, entry in enumerate(res.info)
                    if res.results[i].is_success()
                ]

                if last_data_id + max_queryable - 1 >= late_data_id:
                    # Have seen late entry, can now end if haven't seen anything
                    have_seen_late_data_id = True

                if len(entries) == 0:
                    if have_seen_late_data_id:
                        # End here
                        done_flag.value = True

                        log_lock.acquire()
                        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                        print_and_log(
                            "Finished with metas for process %d" % process_index,
                            log_file,
                        )
                        log_file.close()
                        log_lock.release()
                        break
                else:
                    start_timestamp = common.DateTime.fromtimestamp(
                        entries[-1].create_time.timestamp() - 1
                    ).value()

                    log_lock.acquire()
                    log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Num entries raw %d Num entries filtered %d Last time %s"
                        % (
                            len(entries),
                            len(entries),
                            str(common.DateTime(start_timestamp)),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()

                    # Send these metas off to a open process
                    metas_to_send = [
                        (item.data_id, item.owner_id)
                        for item in entries
                        if item.size > 0
                    ]
                    metas_queue.put(metas_to_send)

                    con.executemany(
                        "INSERT INTO datastore_meta (game, data_id, owner_id, size, name, data_type, meta_binary, permission, delete_permission, create_time, update_time, period, status, referred_count, refer_data_id, flag, referred_time, expire_time) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                pretty_game_id,
                                entry.data_id,
                                str(entry.owner_id),
                                entry.size,
                                entry.name,
                                entry.data_type,
                                entry.meta_binary,
                                entry.permission.permission,
                                entry.delete_permission.permission,
                                timestamp_if_not_null(entry.create_time),
                                timestamp_if_not_null(entry.update_time),
                                entry.period,
                                entry.status,
                                entry.referred_count,
                                entry.refer_data_id,
                                entry.flag,
                                timestamp_if_not_null(entry.referred_time),
                                timestamp_if_not_null(entry.expire_time),
                            )
                            for entry in entries
                        ],
                    )
                    con.executemany(
                        "INSERT INTO datastore_meta_tag (game, data_id, tag) values (?, ?, ?)",
                        [
                            (pretty_game_id, entry.data_id, tag)
                            for entry in entries
                            for tag in entry.tags
                        ],
                    )
                    con.executemany(
                        "INSERT INTO datastore_meta_rating (game, data_id, slot, total_value, count, initial_value) values (?, ?, ?, ?, ?, ?)",
                        [
                            (
                                pretty_game_id,
                                entry.data_id,
                                rating.slot,
                                rating.info.total_value,
                                rating.info.count,
                                rating.info.initial_value,
                            )
                            for entry in entries
                            for rating in entry.ratings
                        ],
                    )
                    con.executemany(
                        "INSERT INTO datastore_permission_recipients (game, data_id, is_delete, recipient) values (?, ?, ?, ?)",
                        [
                            (pretty_game_id, entry.data_id, 0, str(recipient))
                            for entry in entries
                            for recipient in entry.permission.recipients
                        ],
                    )
                    con.executemany(
                        "INSERT INTO datastore_permission_recipients (game, data_id, is_delete, recipient) values (?, ?, ?, ?)",
                        [
                            (pretty_game_id, entry.data_id, 1, str(recipient))
                            for entry in entries
                            for recipient in entry.delete_permission.recipients
                        ],
                    )
                    con.commit()

                last_data_id += max_queryable * total_num_processes

            num_metas_threads_done.value += 1
            if num_metas_threads_done.value == total_num_processes:
                done_flag.value = True
        except Exception as e:
            print("".join(traceback.TracebackException.from_exception(e).format()))

        con.close()

    anyio.run(run)

def get_datastore_metas_pids(
    log_lock,
    access_key,
    nex_version,
    host,
    port,
    pid,
    password,
    pretty_game_id,
    pids_queue,
    s,
    auth_info=None,
):
    async def run():
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)

        try:

            while True:
                time.sleep(0.5)

                try:
                    pids = pids_queue.get(block=False)

                    log_lock.acquire()
                    log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Start download of %d pids" % len(pids), log_file
                    )
                    log_file.close()
                    log_lock.release()

                    download_entries = None
                    try:
                        async def get_res(client):
                            store = datastore.DataStoreClient(client)

                            params = []
                            for entry in pids:
                                param = datastore.DataStoreGetMetaParam()
                                param.persistence_target.owner_id = entry[0]
                                param.persistence_target.persistence_id = entry[1]
                                param.result_option = 0xFF
                                params.append(param)

                            res = await store.get_metas_multiple_param(params)

                            return res

                        res = await retry_if_rmc_error(
                            get_res, s, host, port, str(pid), password, auth_info=auth_info
                        )

                        # Remove invalid and add persistence info
                        meta_entries = [
                            (entry, pids[i])
                            for i, entry in enumerate(res.infos)
                            if res.results[i].is_success()
                        ]

                        con.executemany(
                            "INSERT INTO datastore_persistent (game, owner_id, persistence_id, data_id) values (?, ?, ?, ?)",
                            [
                                (
                                    pretty_game_id,
                                    entry[1][0],
                                    entry[1][1],
                                    entry[0].data_id,
                                )
                                for entry in meta_entries
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_meta (game, data_id, owner_id, size, name, data_type, meta_binary, permission, delete_permission, create_time, update_time, period, status, referred_count, refer_data_id, flag, referred_time, expire_time) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    pretty_game_id,
                                    entry[0].data_id,
                                    str(entry[0].owner_id),
                                    entry[0].size,
                                    entry[0].name,
                                    entry[0].data_type,
                                    entry[0].meta_binary,
                                    entry[0].permission.permission,
                                    entry[0].delete_permission.permission,
                                    timestamp_if_not_null(entry[0].create_time),
                                    timestamp_if_not_null(entry[0].update_time),
                                    entry[0].period,
                                    entry[0].status,
                                    entry[0].referred_count,
                                    entry[0].refer_data_id,
                                    entry[0].flag,
                                    timestamp_if_not_null(entry[0].referred_time),
                                    timestamp_if_not_null(entry[0].expire_time),
                                )
                                for entry in meta_entries
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_meta_tag (game, data_id, tag) values (?, ?, ?)",
                            [
                                (pretty_game_id, entry[0].data_id, tag)
                                for entry in meta_entries
                                for tag in entry[0].tags
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_meta_rating (game, data_id, slot, total_value, count, initial_value) values (?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    pretty_game_id,
                                    entry[0].data_id,
                                    rating.slot,
                                    rating.info.total_value,
                                    rating.info.count,
                                    rating.info.initial_value,
                                )
                                for entry in meta_entries
                                for rating in entry[0].ratings
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_permission_recipients (game, data_id, is_delete, recipient) values (?, ?, ?, ?)",
                            [
                                (pretty_game_id, entry[0].data_id, 0, str(recipient))
                                for entry in meta_entries
                                for recipient in entry[0].permission.recipients
                            ],
                        )
                        con.executemany(
                            "INSERT INTO datastore_permission_recipients (game, data_id, is_delete, recipient) values (?, ?, ?, ?)",
                            [
                                (pretty_game_id, entry[0].data_id, 1, str(recipient))
                                for entry in meta_entries
                                for recipient in entry[0].delete_permission.recipients
                            ],
                        )
                        con.commit()

                        download_entries = [(entry[0].data_id, 0) for entry in meta_entries if entry[0].size > 0]
                    except RMCError as e:
                        log_lock.acquire()
                        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                        print_and_log("Small issue: %s" % str(e), log_file)
                        log_file.close()
                        log_lock.release()

                    for entry in download_entries:
                        try:
                            data_id, owner_id = entry

                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log("Start %d" % data_id, log_file)
                            log_file.close()
                            log_lock.release()

                            start = time.perf_counter()

                            async def get_req_info(client):
                                store = datastore.DataStoreClient(client)

                                get_param = datastore.DataStorePrepareGetParam()
                                get_param.data_id = data_id

                                req_info = await store.prepare_get_object(get_param)
                                headers = {
                                    header.key: header.value
                                    for header in req_info.headers
                                }

                                return (req_info.url, req_info, headers)

                            url, req_info, headers = await retry_if_rmc_error(
                                get_req_info,
                                s,
                                host,
                                port,
                                str(pid),
                                password,
                                auth_info=auth_info,
                            )

                            async with httpx.AsyncClient() as client:
                                response = await client.get(
                                    "https://%s" % req_info.url,
                                    headers=headers,
                                    timeout=(60 * 10),
                                )

                            # TODO store the headers too
                            con.execute(
                                "INSERT INTO datastore_data (game, data_id, url, data) values (?, ?, ?, ?)",
                                (
                                    pretty_game_id,
                                    data_id,
                                    url,
                                    gzip.compress(response.content),
                                ),
                            )
                            con.commit()

                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log(
                                "Downloaded %d in %f seconds"
                                % (data_id, time.perf_counter() - start),
                                log_file,
                            )
                            log_file.close()
                            log_lock.release()

                        except RMCError as e:
                            log_lock.acquire()
                            log_file = open(DATASTORE_LOG, "a", encoding="utf-8")
                            print_and_log("Small issue: %s" % str(e), log_file)
                            log_file.close()
                            log_lock.release()
                except queue.Empty:
                    break
        except Exception as e:
            print("".join(traceback.TracebackException.from_exception(e).format()))

        con.close()

    anyio.run(run)


def print_and_log(text, f):
    print(text)
    f.write("%s\n" % text)
    f.flush()


def timestamp_if_not_null(t):
    if t:
        return t.timestamp()
    else:
        return t


async def add_rankings(
    category,
    s,
    host,
    port,
    pid,
    password,
    log_lock,
    rankings,
    pretty_game_id,
    has_datastore,
    con,
    auth_info=None,
):
    # Since this is part of the datastore scrape instead simply ignore
    if has_datastore and False:
        for entry in rankings.data:
            if entry.param:
                result = None
                try:

                    async def get_res():
                        async with backend.connect(s, host, port) as be:
                            async with be.login(str(pid), password) as client:
                                store = datastore.DataStoreClient(client)

                                get_meta_param = datastore.DataStoreGetMetaParam()
                                get_meta_param.result_option = 4
                                get_meta_param.data_id = entry.param
                                get_meta_param.persistence_target.owner_id = entry.pid

                                result = await store.get_meta(get_meta_param)

                                return result

                    result = await retry_if_rmc_error(get_res)
                except RMCError as e:
                    # Usually nintendo.nex.common.RMCError: Ranking::NotFound, ignore
                    None
                except Exception as e:
                    log_lock.acquire()
                    log_file = open(RANKING_LOG, "a", encoding="utf-8")
                    print_and_log(
                        "Could not download meta param for %d: %s"
                        % (
                            entry.rank,
                            "".join(
                                traceback.TracebackException.from_exception(e).format()
                            ),
                        ),
                        log_file,
                    )
                    log_file.close()
                    log_lock.release()

                response = None
                if result and result.size > 0:
                    try:

                        async def get_res():
                            async with backend.connect(s, host, port) as be:
                                async with be.login(str(pid), password) as client:
                                    store = datastore.DataStoreClient(client)

                                    get_param = datastore.DataStorePrepareGetParam()
                                    get_param.data_id = entry.param
                                    get_param.persistence_target.owner_id = entry.pid

                                    req_info = await store.prepare_get_object(get_param)
                                    headers = {
                                        header.key: header.value
                                        for header in req_info.headers
                                    }

                                    return (req_info, headers)

                        req_info, headers = await retry_if_rmc_error(get_res)
                        response = await http.get(req_info.url, headers=headers)
                        response.raise_if_error()
                    except RMCError as e:
                        # Usually nintendo.nex.common.RMCError: Ranking::NotFound, ignore
                        None
                    except Exception as e:
                        log_lock.acquire()
                        log_file = open(RANKING_LOG, "a", encoding="utf-8")
                        print_and_log(
                            "Could not download param for %d: %s"
                            % (
                                entry.rank,
                                "".join(
                                    traceback.TracebackException.from_exception(
                                        e
                                    ).format()
                                ),
                            ),
                            log_file,
                        )
                        log_file.close()
                        log_lock.release()

                if result:
                    # TODO store more!
                    con.execute(
                        "INSERT INTO ranking_meta (game, pid, rank, category, data_id, size, name, data_type, meta_binary, create_time, update_time) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            pretty_game_id,
                            str(entry.pid),
                            entry.rank,
                            category,
                            result.data_id,
                            result.size,
                            result.name,
                            result.data_type,
                            result.meta_binary,
                            timestamp_if_not_null(result.create_time),
                            timestamp_if_not_null(result.update_time),
                        ),
                    )
                    if result.size > 0:
                        con.execute(
                            "INSERT INTO ranking_param_data (game, pid, rank, category, data) values (?, ?, ?, ?, ?)",
                            (
                                pretty_game_id,
                                str(entry.pid),
                                entry.rank,
                                category,
                                response.body,
                            ),
                        )
                    con.commit()

    con.executemany(
        "INSERT INTO ranking (game, id, pid, rank, category, score, param, data, update_time) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                pretty_game_id,
                str(entry.unique_id),
                str(entry.pid),
                entry.rank,
                entry.category,
                entry.score,
                str(entry.param),
                entry.common_data,
                timestamp_if_not_null(entry.update_time),
            )
            for entry in rankings.data
        ],
    )
    con.executemany(
        "INSERT INTO ranking_group (game, pid, rank, category, ranking_group, ranking_index) values (?, ?, ?, ?, ?, ?)",
        [
            (pretty_game_id, str(entry.pid), entry.rank, category, group, i)
            for entry in rankings.data
            for i, group in enumerate(entry.groups)
        ],
    )
    con.commit()


# NintendoClients does not implement this properly
def new_RankingRankData_load(self, stream, version):
    self.pid = stream.pid()
    self.unique_id = stream.u64()
    self.rank = stream.u32()
    self.category = stream.u32()
    self.score = stream.u32()
    self.groups = stream.list(stream.u8)
    self.param = stream.u64()
    self.common_data = stream.buffer()
    if version >= 1:
        self.update_time = stream.datetime()


# Gets rid of the "unexpected version" warning
def new_RankingRankData_max_version(self, settings):
    return 1


async def scrape_by_data_id(store, start_data_id):
    # Get max number of entries queryable at once
    # Usually 100 but worth trying
    max_queryable = 100
    """
    while True:
        try:
            param = datastore.DataStoreSearchParam()
            param.result_range.offset = 0
            param.result_range.size = max_queryable
            param.result_option = 0xFF
            res = await store.search_object(param)

            if len(res.result) == 0:
                max_queryable -= 1
                break
        except RMCError as e:
            max_queryable -= 1
            break
        else:
            max_queryable += 1
            print("Incrementing to %d" % max_queryable)
    """

    print("Max queryable: %d" % max_queryable)

    start_timestamp = common.DateTime.fromtimestamp(0).value()
    last_seen_data_ids = set()

    while True:
        param = datastore.DataStoreSearchParam()
        param.created_after = common.DateTime(start_timestamp)
        param.created_before = common.DateTime.fromtimestamp(2145942000)
        param.result_range.size = max_queryable
        param.result_option = 0xFF
        res = await store.search_object(param)

        if len(res.result) == 0:
            # Go to get_metas
            break
        else:
            entries = list(
                filter(lambda x: x.data_id not in last_seen_data_ids, res.result)
            )

            if len(entries) == 0:
                # No new, end
                break

            print([item.data_id for item in entries])

            start_timestamp = common.DateTime.fromtimestamp(
                res.result[-1].create_time.timestamp() - 1
            ).value()
            print(str(common.DateTime(start_timestamp)))

            last_seen_data_ids = set([item.data_id for item in res.result])

    """
    while True:
        param = datastore.DataStoreSearchParam()
        param.data_id = 0
        param.result_option = 0xFF
        res = await store.get_metas(list(range(last_data_id, last_data_id + max_queryable)), param)

        print([item.data_id for item in res.info])
        print([item.is_success() for item in res.results])

        if len(res.info) == 0:
            break
        else:
            last_data_id = res.info[-1].data_id
    """


async def search_works(store):
    search_object_works = None
    try:
        param = datastore.DataStoreSearchParam()
        param.result_range.offset = 0
        param.result_range.size = 1
        param.result_option = 0xFF
        # DataStoreSearchResult
        await store.search_object(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            search_object_works = False
        elif e.name() == "DataStore::NotFound":
            search_object_works = True
        else:
            search_object_works = False
    except Exception as e:
        search_object_works = False
    else:
        search_object_works = True

    return search_object_works

    # Preferences
    # 1) get_metas
    # 2) get_specific_meta_v1
    # 3) search_object
    # 4) search_object_light
    # 1) get_object_infos
    # 2)

    try:
        param = datastore.DataStoreGetMetaParam()
        # DataStoreMetaInfo[]
        await store.get_metas([1000000], param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_metas: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_metas: Worked!")
        else:
            print(
                "get_metas:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_metas:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_metas: Worked!")

    try:
        param = datastore.DataStoreSearchParam()
        param.result_range.offset = 0
        param.result_range.size = 1
        param.result_option = 0xFF
        # DataStoreSearchResult
        await store.search_object(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("search_object: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("search_object: Worked!")
        else:
            print(
                "search_object:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "search_object:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("search_object: Worked!")

    try:
        # DataStoreRatingInfoWithSlot
        await store.get_ratings([1000000], 0)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_ratings: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_ratings: Worked!")
        else:
            print(
                "get_ratings:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_ratings:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_ratings: Worked!")

    try:
        param = datastore.DataStoreGetSpecificMetaParamV1()
        param.data_ids = [1000000]
        # DataStoreSpecificMetaInfoV1
        await store.get_specific_meta_v1(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_specific_meta_v1: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_specific_meta_v1: Worked!")
        else:
            print(
                "get_specific_meta_v1:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_specific_meta_v1:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_specific_meta_v1: Worked!")

    try:
        param = datastore.DataStoreRatingTarget()
        param.data_id = 1000000
        param.slot = 0
        # ?
        await store.get_rating_with_log(param, 0)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_rating_with_log: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_rating_with_log: Worked!")
        else:
            print(
                "get_rating_with_log:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_rating_with_log:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_rating_with_log: Worked!")

    # TODO
    # param = datastore.DataStoreGetNewArrivedNotificationsParam()
    # result: DataStoreNotification has_next: bool
    # await store.get_new_arrived_notifications(param)

    try:
        # DataStorePersistenceInfo[]
        await store.get_persistence_infos(1234, [0])
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_persistence_infos: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_persistence_infos: Worked!")
        else:
            print(
                "get_persistence_infos:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_persistence_infos:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_persistence_infos: Worked!")

    try:
        param = datastore.DataStorePrepareGetParam()
        param.data_id = 1000000
        # get_info: DataStoreReqGetInfo additional_meta: DataStoreReqGetAdditionalMeta
        await store.prepare_get_object_or_meta_binary(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("prepare_get_object_or_meta_binary: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("prepare_get_object_or_meta_binary: Worked!")
        else:
            print(
                "prepare_get_object_or_meta_binary:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "prepare_get_object_or_meta_binary:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("prepare_get_object_or_meta_binary: Worked!")

    try:
        param = datastore.DataStorePrepareGetParam()
        param.data_id = 1000000
        # get_info: DataStoreReqGetInfo additional_meta: DataStoreReqGetAdditionalMeta
        await store.prepare_get_object(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("prepare_get_object: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("prepare_get_object: Worked!")
        else:
            print(
                "prepare_get_object:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "prepare_get_object:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("prepare_get_object: Worked!")

    try:
        param = datastore.DataStorePrepareGetParamV1()
        param.data_id = 1000000
        # DataStoreReqGetInfoV1
        await store.prepare_get_object_v1(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("prepare_get_object_v1: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("prepare_get_object_v1: Worked!")
        else:
            print(
                "prepare_get_object_v1:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "prepare_get_object_v1:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("prepare_get_object_v1: Worked!")

    try:
        # infos: DataStorePasswordInfo[]
        await store.get_password_infos([1000000])
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_password_infos: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_password_infos: Worked!")
        else:
            print(
                "get_password_infos:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_password_infos:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_password_infos: Worked!")

    try:
        param = datastore.DataStoreGetMetaParam()
        param.data_id = 1000000
        params = [param]
        # infos: DataStoreMetaInfo[]
        await store.get_metas_multiple_param(params)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_metas_multiple_param: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_metas_multiple_param: Worked!")
        else:
            print(
                "get_metas_multiple_param:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_metas_multiple_param:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_metas_multiple_param: Worked!")

    try:
        # infos: DataStoreReqGetInfo[]
        await store.get_object_infos([1000000])
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("get_object_infos: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("get_object_infos: Worked!")
        else:
            print(
                "get_object_infos:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "get_object_infos:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("get_object_infos: Worked!")

    try:
        param = datastore.DataStoreSearchParam()
        param.result_range.offset = 0
        param.result_range.size = 1
        param.result_option = 0xFF
        # DataStoreSearchResult
        await store.search_object_light(param)
    except RMCError as e:
        if e.name() == "Core::NotImplemented":
            print("search_object_light: Core::NotImplemented")
        elif e.name() == "DataStore::NotFound":
            print("search_object_light: Worked!")
        else:
            print(
                "search_object_light:"
                + "".join(traceback.TracebackException.from_exception(e).format())
            )
    except Exception as e:
        print(
            "search_object_light:"
            + "".join(traceback.TracebackException.from_exception(e).format())
        )
    else:
        print("search_object_light: Worked!")

    return

    # prepare_post_object_v1

    # complete_post_object_v1

    # delete_object

    # delete_objects

    # change_meta_v1

    # change_metas_v1

    param = datastore.DataStoreGetMetaParam()
    # DataStoreMetaInfo
    await store.get_meta(param)

    # prepare_update_object

    # complete_update_object

    param = datastore.DataStoreGetNotificationUrlParam()
    # DataStoreReqGetNotificationUrlInfo
    await store.get_notification_url(param)

    param = datastore.DataStoreGetNewArrivedNotificationsParam()
    # DataStoreNotificationV1
    await store.get_new_arrived_notifications_v1(param)

    # rate_object

    param = datastore.DataStoreRatingTarget()
    # DataStoreRatingInfo
    await store.get_rating(param, 0)

    # reset_rating

    # reset_ratings

    # post_meta_binary

    # touch_object

    # prepare_post_object

    param = datastore.DataStorePrepareGetParam()
    # rating: DataStoreRatingInfo log: DataStoreRatingLog
    await store.prepare_get_object(param)

    # complete_post_object

    param = datastore.DataStoreGetSpecificMetaParam()
    # DataStoreSpecificMetaInfo[]
    await store.get_specific_meta(param)

    # DataStorePersistenceInfo
    await store.get_persistence_info(0, 0)

    # perpetuate_object

    # unperpetuate_object

    # DataStorePasswordInfo
    await store.get_password_info(0)

    # complete_post_objects

    # change_meta

    # change_metas

    # rate_objects

    # post_meta_binary_with_data_id

    # post_meta_binaries_with_data_id

    # rate_object_with_posting

    # rate_objects_with_posting


ranking.RankingRankData.load = new_RankingRankData_load
ranking.RankingRankData.max_version = new_RankingRankData_max_version


async def main():
    if sys.argv[1] == "create":
        con = sqlite3.connect(RANKING_DB, timeout=3600)
        cur = con.cursor()
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking (
        game TEXT NOT NULL,
        id TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        score INTEGER NOT NULL,
        param TEXT NOT NULL,
        data BLOB,
        update_time INTEGER
    )"""
        )
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking_group (
        game TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        ranking_group INTEGER NOT NULL,
        ranking_index INTEGER NOT NULL
    )"""
        )
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking_param_data (
        game TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        data BLOB
    )"""
        )
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking_meta (
        game TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        data_id INTEGER,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        -- TODO add permisions
        create_time INTEGER,
        update_time INTEGER
        -- TODO add tags
        -- TODO add ratings
    )"""
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_ranking_game_category ON ranking (game, category)"""
        )
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_ranking_rank ON ranking (rank)""")

        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(RANKING_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            print_and_log(
                "%s (%d out of %d)"
                % (game["name"].replace("\n", " "), i, len(nex_wiiu_games)),
                log_file,
            )

            pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

            # If anything already exists for this game ignore
            # if len(cur.execute("SELECT rank FROM ranking WHERE game = ? LIMIT 1", (pretty_game_id,)).fetchall()) > 0:
            # 	continue

            nas = nnas.NNASClient()
            nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
            nas.set_title(game["aid"], game["av"])
            nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

            access_token = await nas.login(USERNAME, PASSWORD)

            nex_token = await nas.get_nex_token(access_token.token, game["id"])

            nex_version = (
                game["nex"][0][0] * 10000 + game["nex"][0][1] * 100 + game["nex"][0][2]
            )

            # Check if nexds is loaded
            if game["aid"] == 1407435282983680:
                has_datastore = True
            else:
                has_datastore = bool(
                    [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
                )

            """
            # Run everything in processes
            num_processes = 8
            range_size = int(pow(2, 32) / num_processes)

            found_queue = Queue()
            num_tested_queue = Queue()

            processes = [Process(target=range_test_category,
                args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
            # Queue for printing number tested and found categories
            processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
            for p in processes:
                p.start()
            for p in processes:
                p.join()

            continue
            """

            valid_categories = []
            num_tested = 0

            s = settings.default()
            s.configure(game["key"], nex_version)
            async with backend.connect(s, nex_token.host, nex_token.port) as be:
                async with be.login(str(nex_token.pid), nex_token.password) as client:
                    ranking_client = ranking.RankingClient(client)

                    for category in range(1000):
                        try:
                            order_param = ranking.RankingOrderParam()
                            order_param.offset = 0
                            order_param.count = 1

                            _ = await ranking_client.get_ranking(
                                ranking.RankingMode.GLOBAL,  # Get the global leaderboard
                                category,  # Category, this is 3-A (Magrove Cove)
                                order_param,
                                0,
                                0,
                            )

                            # No exception, this is a valid category
                            valid_categories.append(category)
                            print_and_log("Found category %d" % category, log_file)
                        except Exception:
                            None

                        num_tested += 1

                        if num_tested % 10 == 0:
                            print_and_log("Tested %d categories" % num_tested, log_file)

            if game["aid"] == 1407375153317888:
                valid_categories.extend(
                    [
                        0x5DD7E214,
                        0x13759B11,
                        0xE3123FD0,
                        0x912DF205,
                        0x9E391E6D,
                        0x5B41DCD6,
                        0x403CF15E,
                        0x9A479BC2,
                        0x5A8C9203,
                        0x27E351EC,
                        0xB2F30301,
                        0xC3701F2C,
                        0xC44FE9B2,
                        0x169BCB49,
                        0x893EB726,
                        0x1D46C990,
                        0x428E1F5B,
                        0x421F85DE,
                        0x980638CD,
                        0x9A9E4578,
                        0x529F713C,
                        0x229D4B34,
                        0xDEB25266,
                        0x9F206066,
                        0xAD56AF59,
                        0xDADEB14A,
                        0xA1137287,
                        0xC5BE4809,
                        0xFE7E5473,
                        0x7FE2A8DC,
                        0x80469829,
                        0xD26D5AAA,
                        0xDC9C0EED,
                        0x997FB4A2,
                        0x12C3F595,
                        0xC484D676,
                        0xA002D295,
                        0xECC83B64,
                        0x8A33A8A9,
                        0xF0E2800E,
                        0x431B4770,
                        0x6DBE41C2,
                        0x5CC03A6F,
                        0x13AE214C,
                        0x773204C6,
                        0xC4262903,
                        0xFE5FD35F,
                        0xE4CB3C45,
                        0xADBE9415,
                        0xC0D23671,
                        0xCBDC7006,
                        0x4EEB52FF,
                        0xF03B8ADE,
                        0x6A46C6B5,
                        0x3603775F,
                        0x954ABE27,
                        0xCBB12D65,
                        0x4BEAF6F6,
                        0x37082275,
                        0xD94FD2F6,
                        0xB1C43F16,
                        0xDE28ED26,
                        0xBC0CD164,
                    ]
                )

            subgroup_size = 32
            subgroup_size_groups = [
                valid_categories[i : i + subgroup_size]
                for i in range(0, len(valid_categories), subgroup_size)
            ]

            log_lock = Lock()

            for group in subgroup_size_groups:
                # Run categories in parallel
                processes = [
                    Process(
                        target=run_category_scrape,
                        args=(
                            category,
                            log_lock,
                            s,
                            nex_token.host,
                            nex_token.port,
                            nex_token.pid,
                            nex_token.password,
                            game,
                            pretty_game_id,
                            has_datastore,
                            i,
                            nex_wiiu_games,
                        ),
                    )
                    for category in group
                ]
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

        log_file.close()

    if sys.argv[1] == "create_3ds":
        con = sqlite3.connect(RANKING_DB, timeout=3600)
        cur = con.cursor()
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking (
        game TEXT NOT NULL,
        id TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        score INTEGER NOT NULL,
        param TEXT NOT NULL,
        data BLOB,
        update_time INTEGER
    )"""
        )
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking_group (
        game TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        ranking_group INTEGER NOT NULL,
        ranking_index INTEGER NOT NULL
    )"""
        )
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking_param_data (
        game TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        data BLOB
    )"""
        )
        cur.execute(
            """
    CREATE TABLE IF NOT EXISTS ranking_meta (
        game TEXT NOT NULL,
        pid TEXT NOT NULL,
        rank INTEGER NOT NULL,
        category INTEGER NOT NULL,
        data_id INTEGER,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        -- TODO add permisions
        create_time INTEGER,
        update_time INTEGER
        -- TODO add tags
        -- TODO add ratings
    )"""
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_ranking_game_category ON ranking (game, category)"""
        )
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_ranking_rank ON ranking (rank)""")

        f = open("../../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"]
        f.close()

        log_file = open(RANKING_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_3ds_games):
            print_and_log(
                "%s (%d out of %d)"
                % (game["name"].replace("\n", " "), i, len(nex_3ds_games)),
                log_file,
            )

            pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")
            title_version = (
                game["nex"][0][0] * 10000 + game["nex"][0][1] * 100 + game["nex"][0][2]
            )

            nas = nasc.NASCClient()
            nas.set_title(game["aid"], title_version)
            nas.set_device(SERIAL_NUMBER_3DS, MAC_ADDRESS_3DS, FCD_CERT_3DS, "")
            nas.set_locale(REGION_3DS, LANGUAGE_3DS)
            nas.set_user(USERNAME_3DS, USERNAME_HMAC_3DS)

            try:
                response_token = await nas.login(game["aid"] & 0xFFFFFFFF)
            except Exception as e:
                print_and_log(
                    "".join(traceback.TracebackException.from_exception(e).format()),
                    log_file,
                )
                continue

            class NexToken3DS:
                def __init__(self):
                    self.host = None
                    self.port = None
                    self.pid = None
                    self.password = None

            nex_token = NexToken3DS()
            nex_token.host = response_token.host
            nex_token.port = response_token.port
            nex_token.pid = int(PID_3DS)
            nex_token.password = PASSWORD_3DS

            if game["aid"] == 1125899907040768:
                auth_info = authentication.AuthenticationInfo()
                auth_info.token = nex_token_old.token
                auth_info.ngs_version = 2
            else:
                auth_info = None

            nex_version = (
                game["nex"][0][0] * 10000 + game["nex"][0][1] * 100 + game["nex"][0][2]
            )

            # Check if nexds is loaded
            has_datastore = game["has_datastore"]

            """
            # Run everything in processes
            num_processes = 8
            range_size = int(pow(2, 32) / num_processes)

            found_queue = Queue()
            num_tested_queue = Queue()

            processes = [Process(target=range_test_category,
                args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
            # Queue for printing number tested and found categories
            processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
            for p in processes:
                p.start()
            for p in processes:
                p.join()

            continue
            """

            valid_categories = []
            num_tested = 0

            s = settings.load("3ds")
            s.configure(game["key"], nex_version)
            s["prudp.version"] = 1

            async with backend.connect(s, nex_token.host, nex_token.port) as be:
                async with be.login(
                    str(nex_token.pid), nex_token.password, auth_info=auth_info
                ) as client:
                    ranking_client = ranking.RankingClient(client)

                    for category in range(1000):
                        try:
                            order_param = ranking.RankingOrderParam()
                            order_param.offset = 0
                            order_param.count = 1

                            _ = await ranking_client.get_ranking(
                                ranking.RankingMode.GLOBAL,  # Get the global leaderboard
                                category,  # Category, this is 3-A (Magrove Cove)
                                order_param,
                                0,
                                0,
                            )

                            # No exception, this is a valid category
                            valid_categories.append(category)
                            print_and_log("Found category %d" % category, log_file)
                        except Exception:
                            None

                        num_tested += 1

                        if num_tested % 10 == 0:
                            print_and_log("Tested %d categories" % num_tested, log_file)

            if game["aid"] == 1407375153317888:
                valid_categories.extend(
                    [
                        0x5DD7E214,
                        0x13759B11,
                        0xE3123FD0,
                        0x912DF205,
                        0x9E391E6D,
                        0x5B41DCD6,
                        0x403CF15E,
                        0x9A479BC2,
                        0x5A8C9203,
                        0x27E351EC,
                        0xB2F30301,
                        0xC3701F2C,
                        0xC44FE9B2,
                        0x169BCB49,
                        0x893EB726,
                        0x1D46C990,
                        0x428E1F5B,
                        0x421F85DE,
                        0x980638CD,
                        0x9A9E4578,
                        0x529F713C,
                        0x229D4B34,
                        0xDEB25266,
                        0x9F206066,
                        0xAD56AF59,
                        0xDADEB14A,
                        0xA1137287,
                        0xC5BE4809,
                        0xFE7E5473,
                        0x7FE2A8DC,
                        0x80469829,
                        0xD26D5AAA,
                        0xDC9C0EED,
                        0x997FB4A2,
                        0x12C3F595,
                        0xC484D676,
                        0xA002D295,
                        0xECC83B64,
                        0x8A33A8A9,
                        0xF0E2800E,
                        0x431B4770,
                        0x6DBE41C2,
                        0x5CC03A6F,
                        0x13AE214C,
                        0x773204C6,
                        0xC4262903,
                        0xFE5FD35F,
                        0xE4CB3C45,
                        0xADBE9415,
                        0xC0D23671,
                        0xCBDC7006,
                        0x4EEB52FF,
                        0xF03B8ADE,
                        0x6A46C6B5,
                        0x3603775F,
                        0x954ABE27,
                        0xCBB12D65,
                        0x4BEAF6F6,
                        0x37082275,
                        0xD94FD2F6,
                        0xB1C43F16,
                        0xDE28ED26,
                        0xBC0CD164,
                    ]
                )

            subgroup_size = 32
            subgroup_size_groups = [
                valid_categories[i : i + subgroup_size]
                for i in range(0, len(valid_categories), subgroup_size)
            ]

            log_lock = Lock()

            for group in subgroup_size_groups:
                # Run categories in parallel
                processes = [
                    Process(
                        target=run_category_scrape,
                        args=(
                            category,
                            log_lock,
                            s,
                            nex_token.host,
                            nex_token.port,
                            nex_token.pid,
                            nex_token.password,
                            game,
                            pretty_game_id,
                            has_datastore,
                            i,
                            nex_3ds_games,
                            auth_info,
                        ),
                    )
                    for category in group
                ]
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

        log_file.close()

    if sys.argv[1] == "datastore_from_ranking_3ds":
        ranking_con = sqlite3.connect(RANKING_DB, timeout=3600)
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        f = open("../../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"]
        f.close()

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_3ds_games):
            # Check if nexds is loaded
            has_datastore = game["has_datastore"]

            if has_datastore:
                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")
                title_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                nas = nasc.NASCClient()
                nas.set_title(game["aid"], title_version)
                nas.set_device(SERIAL_NUMBER_3DS, MAC_ADDRESS_3DS, FCD_CERT_3DS, "")
                nas.set_locale(REGION_3DS, LANGUAGE_3DS)
                nas.set_user(USERNAME_3DS, USERNAME_HMAC_3DS)

                nex_token_old = await nas.login(game["aid"] & 0xFFFFFFFF)

                class NexToken3DS:
                    def __init__(self):
                        self.host = None
                        self.port = None
                        self.pid = None
                        self.password = None

                nex_token = NexToken3DS()
                nex_token.host = nex_token_old.host
                nex_token.port = nex_token_old.port
                nex_token.pid = int(PID_3DS)
                nex_token.password = PASSWORD_3DS

                if game["aid"] == 1125899907040768:
                    auth_info = authentication.AuthenticationInfo()
                    auth_info.token = nex_token_old.token
                    auth_info.ngs_version = 2
                else:
                    auth_info = None

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                s = settings.load("3ds")
                s.configure(game["key"], nex_version)

                # Get everything to download
                download_entries = (
                            ranking_con.cursor()
                            .execute(
                                "SELECT param, 0 as owner_id FROM ranking WHERE game = ?",
                                (pretty_game_id,),
                            )
                            .fetchall()
                        )
                
                print_and_log(
                    "%s done reading from DB" % game["name"].replace("\n", " "),
                    log_file,
                )

                num_download_threads = 16

                log_lock = Lock()
                metas_queue = Queue()
                done_flag = Value("i", True)

                while True:
                    metas_queue.put(
                        [
                            (int(entry[0]), int(entry[1]))
                            for entry in download_entries[:100]
                        ]
                    )
                    download_entries = download_entries[100:]
                    if len(download_entries) == 0:
                        break

                processes = []
                for i in range(num_download_threads):
                    processes.append(
                        Process(
                            target=get_datastore_data_and_metas,
                            args=(
                                log_lock,
                                game["key"],
                                nex_version,
                                nex_token.host,
                                nex_token.port,
                                nex_token.pid,
                                nex_token.password,
                                pretty_game_id,
                                metas_queue,
                                done_flag,
                                s,
                                auth_info
                            ),
                        )
                    )

                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

        log_file.close()

    if sys.argv[1] == "fix_meta_binary":
        None

    if sys.argv[1] == "datastore_get_info":
        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            # Check if nexds is loaded
            has_datastore = bool(
                [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
            )

            if has_datastore:
                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

                print(pretty_game_id)

                nas = nnas.NNASClient()
                nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
                nas.set_title(game["aid"], game["av"])
                nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

                access_token = await nas.login(USERNAME, PASSWORD)

                nex_token = await nas.get_nex_token(access_token.token, game["id"])

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.default()
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        first_data_id = None
                        first_data_id_create_time = None
                        if len(res.result) > 0:
                            first_data_id = res.result[0].data_id
                            first_data_id_create_time = res.result[0].create_time

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        param.result_order = 1
                        res = await store.search_object(param)

                        last_data_id = None
                        last_data_id_create_time = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                            last_data_id_create_time = res.result[0].create_time

                        return (
                            first_data_id,
                            first_data_id_create_time,
                            last_data_id,
                            last_data_id_create_time,
                        )

                    (
                        first_data_id,
                        first_data_id_create_time,
                        last_data_id,
                        last_data_id_create_time,
                    ) = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                    )

                    if (
                        first_data_id is not None
                        and first_data_id_create_time is not None
                        and last_data_id is not None
                        and last_data_id_create_time is not None
                    ):
                        print_and_log(
                            "%s,%d,%d,%d,%d"
                            % (
                                pretty_game_id,
                                first_data_id,
                                first_data_id_create_time.timestamp(),
                                last_data_id,
                                last_data_id_create_time.timestamp(),
                            ),
                            log_file,
                        )

        log_file.close()

    if sys.argv[1] == "datastore_get_info_3ds":
        f = open("../../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"]
        f.close()

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_3ds_games):
            # Check if nexds is loaded
            has_datastore = game["has_datastore"]

            if has_datastore:
                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")
                title_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                print(pretty_game_id, title_version)

                nas = nasc.NASCClient()
                nas.set_title(game["aid"], title_version)
                nas.set_device(SERIAL_NUMBER_3DS, MAC_ADDRESS_3DS, FCD_CERT_3DS, "")
                nas.set_locale(REGION_3DS, LANGUAGE_3DS)
                nas.set_user(USERNAME_3DS, USERNAME_HMAC_3DS)

                nex_token_old = await nas.login(game["aid"] & 0xFFFFFFFF)

                class NexToken3DS:
                    def __init__(self):
                        self.host = None
                        self.port = None
                        self.pid = None
                        self.password = None

                nex_token = NexToken3DS()
                nex_token.host = nex_token_old.host
                nex_token.port = nex_token_old.port
                nex_token.pid = int(PID_3DS)
                nex_token.password = PASSWORD_3DS

                if game["aid"] == 1125899907040768:
                    auth_info = authentication.AuthenticationInfo()
                    auth_info.token = nex_token_old.token
                    auth_info.ngs_version = 2
                else:
                    auth_info = None

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.load("3ds")
                s.configure(game["key"], nex_version)

                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                    auth_info=auth_info
                ):

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        first_data_id = None
                        first_data_id_create_time = None
                        if len(res.result) > 0:
                            first_data_id = res.result[0].data_id
                            first_data_id_create_time = res.result[0].create_time

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        param.result_order = 1
                        res = await store.search_object(param)

                        last_data_id = None
                        last_data_id_create_time = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                            last_data_id_create_time = res.result[0].create_time

                        return (
                            first_data_id,
                            first_data_id_create_time,
                            last_data_id,
                            last_data_id_create_time,
                        )

                    (
                        first_data_id,
                        first_data_id_create_time,
                        last_data_id,
                        last_data_id_create_time,
                    ) = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                        auth_info=auth_info
                    )

                    if (
                        first_data_id is not None
                        and first_data_id_create_time is not None
                        and last_data_id is not None
                        and last_data_id_create_time is not None
                    ):
                        print_and_log(
                            "%s,%d,%d,%d,%d"
                            % (
                                pretty_game_id,
                                first_data_id,
                                first_data_id_create_time.timestamp(),
                                last_data_id,
                                last_data_id_create_time.timestamp(),
                            ),
                            log_file,
                        )

        log_file.close()

    if sys.argv[1] == "datastore_just_metas":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            # Check if nexds is loaded
            has_datastore = bool(
                [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
            )

            if has_datastore:
                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

                nas = nnas.NNASClient()
                nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
                nas.set_title(game["aid"], game["av"])
                nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

                access_token = await nas.login(USERNAME, PASSWORD)

                nex_token = await nas.get_nex_token(access_token.token, game["id"])

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.default()
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        first_data_id = None
                        first_data_id_create_time = None
                        if len(res.result) > 0:
                            first_data_id = res.result[0].data_id
                            first_data_id_create_time = res.result[0].create_time

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        param.result_order = 1
                        res = await store.search_object(param)

                        last_data_id = None
                        last_data_id_create_time = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                            last_data_id_create_time = res.result[0].create_time

                        return (
                            first_data_id,
                            first_data_id_create_time,
                            last_data_id,
                            last_data_id_create_time,
                        )

                    (
                        first_data_id,
                        first_data_id_create_time,
                        last_data_id,
                        last_data_id_create_time,
                    ) = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                    )

                    if (
                        first_data_id is not None
                        and first_data_id_create_time is not None
                        and last_data_id is not None
                        and last_data_id_create_time is not None
                    ):
                        None

        log_file.close()

    if sys.argv[1] == "datastore_just_metas_3ds":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        f = open("../../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"]
        f.close()

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_3ds_games):
            # Check if nexds is loaded
            has_datastore = game["has_datastore"]

            if has_datastore:
                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")
                title_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                nas = nasc.NASCClient()
                nas.set_title(game["aid"], title_version)
                nas.set_device(SERIAL_NUMBER_3DS, MAC_ADDRESS_3DS, FCD_CERT_3DS, "")
                nas.set_locale(REGION_3DS, LANGUAGE_3DS)
                nas.set_user(USERNAME_3DS, USERNAME_HMAC_3DS)

                nex_token_old = await nas.login(game["aid"] & 0xFFFFFFFF)

                class NexToken3DS:
                    def __init__(self):
                        self.host = None
                        self.port = None
                        self.pid = None
                        self.password = None

                nex_token = NexToken3DS()
                nex_token.host = nex_token_old.host
                nex_token.port = nex_token_old.port
                nex_token.pid = int(PID_3DS)
                nex_token.password = PASSWORD_3DS

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.load("3ds")
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        first_data_id = None
                        first_data_id_create_time = None
                        if len(res.result) > 0:
                            first_data_id = res.result[0].data_id
                            first_data_id_create_time = res.result[0].create_time

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        param.result_order = 1
                        res = await store.search_object(param)

                        last_data_id = None
                        last_data_id_create_time = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                            last_data_id_create_time = res.result[0].create_time

                        return (
                            first_data_id,
                            first_data_id_create_time,
                            last_data_id,
                            last_data_id_create_time,
                        )

                    (
                        first_data_id,
                        first_data_id_create_time,
                        last_data_id,
                        last_data_id_create_time,
                    ) = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                    )

                    if (
                        first_data_id is not None
                        and first_data_id_create_time is not None
                        and last_data_id is not None
                        and last_data_id_create_time is not None
                    ):
                        None

        log_file.close()

    if sys.argv[1] == "datastore":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"][int(sys.argv[3]) :]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            if i == int(sys.argv[4]):
                print("Reached intended end")
                break

            # Check if nexds is loaded
            if game["aid"] == 1407435282983680:
                has_datastore = True
            else:
                has_datastore = bool(
                    [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
                )

            if has_datastore:
                print_and_log(
                    "%s (%d out of %d)"
                    % (
                        game["name"].replace("\n", " "),
                        i + int(sys.argv[3]),
                        len(nex_wiiu_games),
                    ),
                    log_file,
                )

                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

                nas = nnas.NNASClient()
                nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
                nas.set_title(game["aid"], game["av"])
                nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

                access_token = await nas.login(USERNAME, PASSWORD)

                nex_token = await nas.get_nex_token(access_token.token, game["id"])

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                """
                # Run everything in processes
                num_processes = 8
                range_size = int(pow(2, 32) / num_processes)

                found_queue = Queue()
                num_tested_queue = Queue()

                processes = [Process(target=range_test_category,
                    args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
                # Queue for printing number tested and found categories
                processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

                continue
                """

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.default()
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):
                    print_and_log(
                        "%s DOES support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

                    max_queryable = 100

                    max_entry = (
                        con.cursor()
                        .execute(
                            "SELECT MAX(data_id) FROM datastore_meta WHERE game = ?",
                            (pretty_game_id,),
                        )
                        .fetchall()
                    )
                    max_entry_data_id = None
                    if (
                        max_entry is not None
                        and len(max_entry) > 0
                        and max_entry[0][0] is not None
                    ):
                        max_entry_data_id = int(max_entry[0][0])
                    else:
                        max_entry_data_id = 0

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        last_data_id = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                        else:
                            # Try timestamp method from 2012 as a backup
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                1325401200
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                last_data_id = res.result[0].data_id

                        if last_data_id is None or last_data_id > 900000:
                            # Just start here anyway lol
                            last_data_id = 900000

                        late_time = None
                        late_data_id = None
                        timestamp = int(time.time())
                        while True:
                            # Try to find reasonable time going back, starting at current time
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                timestamp
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                late_time = res.result[0].create_time
                                late_data_id = res.result[0].data_id
                                break
                            elif timestamp > 1325401200:
                                # Take off 1 month
                                timestamp -= 2629800
                            else:
                                # Otherwise timestamp is less than 2012, give up
                                break

                        return (last_data_id, late_time, late_data_id)

                    last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                    )

                    if last_data_id == None or last_data_id < max_entry_data_id:
                        last_data_id = max_entry_data_id

                    if last_data_id is not None and late_data_id is not None:
                        print_and_log(
                            "First data id %d Late time %s Late data ID %d"
                            % (last_data_id, str(late_time), late_data_id),
                            log_file,
                        )

                        # Get all data IDs to download (earlier data IDs)
                        download_entries = (
                            con.cursor()
                            .execute(
                                "SELECT datastore_meta.data_id, owner_id FROM datastore_meta LEFT JOIN datastore_data ON datastore_meta.data_id = datastore_data.data_id WHERE datastore_meta.game = ? AND size > 0 AND data IS NULL",
                                (pretty_game_id,),
                            )
                            .fetchall()
                        )

                        print_and_log(
                            "%s done reading from DB" % game["name"].replace("\n", " "),
                            log_file,
                        )

                        num_metas_threads = 8
                        num_download_threads = 8

                        log_lock = Lock()
                        metas_queue = Queue()
                        done_flag = Value("i", False)
                        num_metas_threads_done = Value("i", 0)

                        while True:
                            metas_queue.put(
                                [
                                    (int(entry[0]), int(entry[1]))
                                    for entry in download_entries[:100]
                                ]
                            )
                            download_entries = download_entries[100:]
                            if len(download_entries) == 0:
                                break

                        processes = []
                        for i in range(num_metas_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_metas,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                        i,
                                        num_metas_threads,
                                        max_queryable,
                                        last_data_id,
                                        late_data_id,
                                        num_metas_threads_done,
                                    ),
                                )
                            )
                        for i in range(num_download_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_data,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                    ),
                                )
                            )

                        for p in processes:
                            p.start()
                        for p in processes:
                            p.join()

                else:
                    print_and_log(
                        "%s does not support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

        log_file.close()

    if sys.argv[1] == "datastore_sampling":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"][int(sys.argv[3]) :]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            if i == int(sys.argv[4]):
                print("Reached intended end")
                break

            # Check if nexds is loaded
            has_datastore = bool(
                [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
            )

            if has_datastore:
                print_and_log(
                    "%s (%d out of %d)"
                    % (
                        game["name"].replace("\n", " "),
                        i + int(sys.argv[3]),
                        len(nex_wiiu_games),
                    ),
                    log_file,
                )

                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

                nas = nnas.NNASClient()
                nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
                nas.set_title(game["aid"], game["av"])
                nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

                access_token = await nas.login(USERNAME, PASSWORD)

                nex_token = await nas.get_nex_token(access_token.token, game["id"])

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                """
                # Run everything in processes
                num_processes = 8
                range_size = int(pow(2, 32) / num_processes)

                found_queue = Queue()
                num_tested_queue = Queue()

                processes = [Process(target=range_test_category,
                    args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
                # Queue for printing number tested and found categories
                processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

                continue
                """

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.default()
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):
                    print_and_log(
                        "%s DOES support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

                    max_queryable = 100

                    max_entry = (
                        con.cursor()
                        .execute(
                            "SELECT MAX(data_id) FROM datastore_meta WHERE game = ?",
                            (pretty_game_id,),
                        )
                        .fetchall()
                    )
                    max_entry_data_id = None
                    if (
                        max_entry is not None
                        and len(max_entry) > 0
                        and max_entry[0][0] is not None
                    ):
                        max_entry_data_id = int(max_entry[0][0])
                    else:
                        max_entry_data_id = 0

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        last_data_id = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                        else:
                            # Try timestamp method from 2012 as a backup
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                1325401200
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                last_data_id = res.result[0].data_id

                        if last_data_id is None:
                            return (None, None, None)

                        if last_data_id is None or last_data_id > 900000:
                            # Just start here anyway lol
                            last_data_id = 900000

                        late_time = None
                        late_data_id = None
                        timestamp = int(time.time())
                        while True:
                            # Try to find reasonable time going back, starting at current time
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                timestamp
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                late_time = res.result[0].create_time
                                late_data_id = res.result[0].data_id
                                break
                            elif timestamp > 1325401200:
                                # Take off 1 month
                                timestamp -= 2629800
                            else:
                                # Otherwise timestamp is less than 2012, give up
                                break

                        if late_data_id > (last_data_id + 200000):
                            late_data_id = last_data_id + 200000

                        return (last_data_id, late_time, late_data_id)

                    last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                    )

                    if last_data_id == None or last_data_id < max_entry_data_id:
                        last_data_id = max_entry_data_id

                    if last_data_id is not None and late_data_id is not None:
                        print_and_log(
                            "First data id %d Late time %s Late data ID %d"
                            % (last_data_id, str(late_time), late_data_id),
                            log_file,
                        )

                        # Get all data IDs to download (earlier data IDs)
                        download_entries = (
                            con.cursor()
                            .execute(
                                "SELECT datastore_meta.data_id, owner_id FROM datastore_meta LEFT JOIN datastore_data ON datastore_meta.data_id = datastore_data.data_id WHERE datastore_meta.game = ? AND size > 0 AND data IS NULL",
                                (pretty_game_id,),
                            )
                            .fetchall()
                        )

                        print_and_log(
                            "%s done reading from DB" % game["name"].replace("\n", " "),
                            log_file,
                        )

                        num_metas_threads = 8
                        num_download_threads = 8

                        log_lock = Lock()
                        metas_queue = Queue()
                        done_flag = Value("i", False)
                        num_metas_threads_done = Value("i", 0)

                        while True:
                            metas_queue.put(
                                [
                                    (int(entry[0]), int(entry[1]))
                                    for entry in download_entries[:100]
                                ]
                            )
                            download_entries = download_entries[100:]
                            if len(download_entries) == 0:
                                break

                        processes = []
                        for i in range(num_metas_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_metas,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                        i,
                                        num_metas_threads,
                                        max_queryable,
                                        last_data_id,
                                        late_data_id,
                                        num_metas_threads_done,
                                    ),
                                )
                            )
                        for i in range(num_download_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_data,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                    ),
                                )
                            )

                        for p in processes:
                            p.start()
                        for p in processes:
                            p.join()

                else:
                    print_and_log(
                        "%s does not support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

        log_file.close()

    if sys.argv[1] == "datastore_use_db":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"][int(sys.argv[3]) :]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            if i == int(sys.argv[4]):
                print("Reached intended end")
                break

            # Check if nexds is loaded
            has_datastore = bool(
                [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
            )

            if has_datastore:
                print_and_log(
                    "%s (%d out of %d)"
                    % (
                        game["name"].replace("\n", " "),
                        i + int(sys.argv[3]),
                        len(nex_wiiu_games),
                    ),
                    log_file,
                )

                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

                nas = nnas.NNASClient()
                nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
                nas.set_title(game["aid"], game["av"])
                nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

                access_token = await nas.login(USERNAME, PASSWORD)

                nex_token = await nas.get_nex_token(access_token.token, game["id"])

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                """
                # Run everything in processes
                num_processes = 8
                range_size = int(pow(2, 32) / num_processes)

                found_queue = Queue()
                num_tested_queue = Queue()

                processes = [Process(target=range_test_category,
                    args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
                # Queue for printing number tested and found categories
                processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

                continue
                """

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.default()
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):
                    print_and_log(
                        "%s DOES support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

                    max_queryable = 100

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        last_data_id = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                        else:
                            # Try timestamp method from 2012 as a backup
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                1325401200
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                last_data_id = res.result[0].data_id

                        if last_data_id is None or last_data_id > 900000:
                            # Just start here anyway lol
                            last_data_id = 900000

                        late_time = None
                        late_data_id = None
                        timestamp = int(time.time())
                        while True:
                            # Try to find reasonable time going back, starting at current time
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                timestamp
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                late_time = res.result[0].create_time
                                late_data_id = res.result[0].data_id
                                break
                            elif timestamp > 1325401200:
                                # Take off 1 month
                                timestamp -= 2629800
                            else:
                                # Otherwise timestamp is less than 2012, give up
                                break

                        return (last_data_id, late_time, late_data_id)

                    last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                    )

                    if last_data_id is not None and late_data_id is not None:
                        print_and_log(
                            "First data id %d Late time %s Late data ID %d"
                            % (last_data_id, str(late_time), late_data_id),
                            log_file,
                        )

                        num_download_threads = 16

                        log_lock = Lock()
                        metas_queue = Queue()
                        done_flag = Value("i", True)
                        num_metas_threads_done = Value("i", 0)

                        # Get all data IDs to download
                        entries = (
                            con.cursor()
                            .execute(
                                "SELECT datastore_meta.data_id, owner_id FROM datastore_meta LEFT JOIN datastore_data ON datastore_meta.data_id = datastore_data.data_id WHERE datastore_meta.game = ? AND size > 0 AND data IS NULL",
                                (pretty_game_id,),
                            )
                            .fetchall()
                        )

                        print_and_log(
                            "%s done reading from DB" % game["name"].replace("\n", " "),
                            log_file,
                        )

                        processes = []
                        for i in range(num_download_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_data,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                    ),
                                )
                            )

                        for p in processes:
                            p.start()

                        while True:
                            metas_queue.put(
                                [
                                    (int(entry[0]), int(entry[1]))
                                    for entry in entries[:100]
                                ]
                            )
                            entries = entries[100:]

                            if len(entries) == 0:
                                break

                        for p in processes:
                            p.join()

                else:
                    print_and_log(
                        "%s does not support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

        log_file.close()

    if sys.argv[1] == "datastore_3ds":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )

        f = open("../../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"][int(sys.argv[3]) :]
        f.close()

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_3ds_games):
            if i == int(sys.argv[4]):
                print("Reached intended end")
                break

            # Check if nexds is loaded
            has_datastore = game["has_datastore"]

            if has_datastore:
                print_and_log(
                    "%s (%d out of %d)"
                    % (
                        game["name"].replace("\n", " "),
                        i + int(sys.argv[3]),
                        len(nex_3ds_games),
                    ),
                    log_file,
                )

                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")
                title_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                nas = nasc.NASCClient()
                nas.set_title(game["aid"], title_version)
                nas.set_device(SERIAL_NUMBER_3DS, MAC_ADDRESS_3DS, FCD_CERT_3DS, "")
                nas.set_locale(REGION_3DS, LANGUAGE_3DS)
                nas.set_user(USERNAME_3DS, USERNAME_HMAC_3DS)

                nex_token_old = await nas.login(game["aid"] & 0xFFFFFFFF)

                class NexToken3DS:
                    def __init__(self):
                        self.host = None
                        self.port = None
                        self.pid = None
                        self.password = None

                nex_token = NexToken3DS()
                nex_token.host = nex_token_old.host
                nex_token.port = nex_token_old.port
                nex_token.pid = int(PID_3DS)
                nex_token.password = PASSWORD_3DS

                if game["aid"] == 1125899907040768:
                    auth_info = authentication.AuthenticationInfo()
                    auth_info.token = nex_token_old.token
                    auth_info.ngs_version = 2
                else:
                    auth_info = None

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                """
                # Run everything in processes
                num_processes = 8
                range_size = int(pow(2, 32) / num_processes)

                found_queue = Queue()
                num_tested_queue = Queue()

                processes = [Process(target=range_test_category,
                    args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
                # Queue for printing number tested and found categories
                processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

                continue
                """

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.load("3ds")
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                    auth_info=auth_info,
                ):
                    print_and_log(
                        "%s DOES support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

                    max_queryable = 100

                    max_entry = (
                        con.cursor()
                        .execute(
                            "SELECT MAX(data_id) FROM datastore_meta WHERE game = ?",
                            (pretty_game_id,),
                        )
                        .fetchall()
                    )
                    max_entry_data_id = None
                    if (
                        max_entry is not None
                        and len(max_entry) > 0
                        and max_entry[0][0] is not None
                    ):
                        max_entry_data_id = int(max_entry[0][0])
                    else:
                        max_entry_data_id = 0

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        last_data_id = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                        else:
                            # Try timestamp method from 2012 as a backup
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                1325401200
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                last_data_id = res.result[0].data_id

                        late_time = None
                        late_data_id = None
                        timestamp = int(time.time())
                        while True:
                            # Try to find reasonable time going back, starting at current time
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                timestamp
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                late_time = res.result[0].create_time
                                late_data_id = res.result[0].data_id
                                break
                            elif timestamp > 1325401200:
                                # Take off 1 month
                                timestamp -= 2629800
                            else:
                                # Otherwise timestamp is less than 2012, give up
                                break

                        return (last_data_id, late_time, late_data_id)

                    last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                        auth_info=auth_info,
                    )

                    if last_data_id is not None and late_data_id is not None:
                        if last_data_id == None or last_data_id < max_entry_data_id:
                            last_data_id = max_entry_data_id

                        print_and_log(
                            "First data id %d Late time %s Late data ID %d"
                            % (last_data_id, str(late_time), late_data_id),
                            log_file,
                        )

                        # Get all data IDs to download (earlier data IDs)
                        download_entries = (
                            con.cursor()
                            .execute(
                                "SELECT datastore_meta.data_id, owner_id FROM datastore_meta LEFT JOIN datastore_data ON datastore_meta.data_id = datastore_data.data_id WHERE datastore_meta.game = ? AND size > 0 AND data IS NULL",
                                (pretty_game_id,),
                            )
                            .fetchall()
                        )

                        print_and_log(
                            "%s done reading from DB" % game["name"].replace("\n", " "),
                            log_file,
                        )

                        num_metas_threads = 8
                        num_download_threads = 8

                        log_lock = Lock()
                        metas_queue = Queue()
                        done_flag = Value("i", False)
                        num_metas_threads_done = Value("i", 0)

                        while True:
                            metas_queue.put(
                                [
                                    (int(entry[0]), int(entry[1]))
                                    for entry in download_entries[:100]
                                ]
                            )
                            download_entries = download_entries[100:]
                            if len(download_entries) == 0:
                                break

                        processes = []
                        for i in range(num_metas_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_metas,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                        i,
                                        num_metas_threads,
                                        max_queryable,
                                        last_data_id,
                                        late_data_id,
                                        num_metas_threads_done,
                                        auth_info,
                                    ),
                                )
                            )
                        for i in range(num_download_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_data,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                        auth_info,
                                    ),
                                )
                            )

                        for p in processes:
                            p.start()
                        for p in processes:
                            p.join()

                else:
                    print_and_log(
                        "%s does not support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

        log_file.close()

    if sys.argv[1] == "datastore_sampling_3ds":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )

        f = open("../../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"][int(sys.argv[3]) :]
        f.close()

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_3ds_games):
            if i == int(sys.argv[4]):
                print("Reached intended end")
                break

            # Check if nexds is loaded
            has_datastore = game["has_datastore"]

            if has_datastore:
                print_and_log(
                    "%s (%d out of %d)"
                    % (
                        game["name"].replace("\n", " "),
                        i + int(sys.argv[3]),
                        len(nex_3ds_games),
                    ),
                    log_file,
                )

                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")
                title_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                nas = nasc.NASCClient()
                nas.set_title(game["aid"], title_version)
                nas.set_device(SERIAL_NUMBER_3DS, MAC_ADDRESS_3DS, FCD_CERT_3DS, "")
                nas.set_locale(REGION_3DS, LANGUAGE_3DS)
                nas.set_user(USERNAME_3DS, USERNAME_HMAC_3DS)

                nex_token_old = await nas.login(game["aid"] & 0xFFFFFFFF)

                class NexToken3DS:
                    def __init__(self):
                        self.host = None
                        self.port = None
                        self.pid = None
                        self.password = None

                nex_token = NexToken3DS()
                nex_token.host = nex_token_old.host
                nex_token.port = nex_token_old.port
                nex_token.pid = int(PID_3DS)
                nex_token.password = PASSWORD_3DS

                if game["aid"] == 1125899907040768:
                    auth_info = authentication.AuthenticationInfo()
                    auth_info.token = nex_token_old.token
                    auth_info.ngs_version = 2
                else:
                    auth_info = None

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                """
                # Run everything in processes
                num_processes = 8
                range_size = int(pow(2, 32) / num_processes)

                found_queue = Queue()
                num_tested_queue = Queue()

                processes = [Process(target=range_test_category,
                    args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
                # Queue for printing number tested and found categories
                processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

                continue
                """

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.load("3ds")
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                    auth_info=auth_info
                ):
                    print_and_log(
                        "%s DOES support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

                    max_queryable = 100

                    max_entry = (
                        con.cursor()
                        .execute(
                            "SELECT MAX(data_id) FROM datastore_meta WHERE game = ?",
                            (pretty_game_id,),
                        )
                        .fetchall()
                    )
                    max_entry_data_id = None
                    if (
                        max_entry is not None
                        and len(max_entry) > 0
                        and max_entry[0][0] is not None
                    ):
                        max_entry_data_id = int(max_entry[0][0])
                    else:
                        max_entry_data_id = 0

                    async def get_initial_data(client):
                        store = datastore.DataStoreClient(client)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        res = await store.search_object(param)

                        last_data_id = None
                        if len(res.result) > 0:
                            last_data_id = res.result[0].data_id
                        else:
                            # Try timestamp method from 2012 as a backup
                            param = datastore.DataStoreSearchParam()
                            param.created_after = common.DateTime.fromtimestamp(
                                1325401200
                            )
                            param.result_range.size = 1
                            param.result_option = 0xFF
                            res = await store.search_object(param)

                            if len(res.result) > 0:
                                last_data_id = res.result[0].data_id

                        if last_data_id is None:
                            return (None, None, None)

                        param = datastore.DataStoreSearchParam()
                        param.result_range.offset = 0
                        param.result_range.size = 1
                        param.result_option = 0xFF
                        param.result_order = 1
                        res = await store.search_object(param)

                        late_time = None
                        late_data_id = None
                        if len(res.result) > 0:
                            late_data_id = res.result[0].data_id
                            late_time = res.result[0].create_time

                        if late_data_id > (last_data_id + 200000):
                            late_data_id = last_data_id + 200000

                        return (last_data_id, late_time, late_data_id)

                    last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                        get_initial_data,
                        s,
                        nex_token.host,
                        nex_token.port,
                        str(nex_token.pid),
                        nex_token.password,
                        auth_info=auth_info
                    )

                    if last_data_id is not None and late_data_id is not None:
                        if last_data_id == None or last_data_id < max_entry_data_id:
                            last_data_id = max_entry_data_id

                        print_and_log(
                            "First data id %d Late time %s Late data ID %d"
                            % (last_data_id, str(late_time), late_data_id),
                            log_file,
                        )

                        # Get all data IDs to download (earlier data IDs)
                        download_entries = (
                            con.cursor()
                            .execute(
                                "SELECT datastore_meta.data_id, owner_id FROM datastore_meta LEFT JOIN datastore_data ON datastore_meta.data_id = datastore_data.data_id WHERE datastore_meta.game = ? AND size > 0 AND data IS NULL",
                                (pretty_game_id,),
                            )
                            .fetchall()
                        )

                        print_and_log(
                            "%s done reading from DB" % game["name"].replace("\n", " "),
                            log_file,
                        )

                        num_metas_threads = 8
                        num_download_threads = 8

                        log_lock = Lock()
                        metas_queue = Queue()
                        done_flag = Value("i", False)
                        num_metas_threads_done = Value("i", 0)

                        while True:
                            metas_queue.put(
                                [
                                    (int(entry[0]), int(entry[1]))
                                    for entry in download_entries[:100]
                                ]
                            )
                            download_entries = download_entries[100:]
                            if len(download_entries) == 0:
                                break

                        processes = []
                        for i in range(num_metas_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_metas,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                        i,
                                        num_metas_threads,
                                        max_queryable,
                                        last_data_id,
                                        late_data_id,
                                        num_metas_threads_done,
                                        auth_info
                                    ),
                                )
                            )
                        for i in range(num_download_threads):
                            processes.append(
                                Process(
                                    target=get_datastore_data,
                                    args=(
                                        log_lock,
                                        game["key"],
                                        nex_version,
                                        nex_token.host,
                                        nex_token.port,
                                        nex_token.pid,
                                        nex_token.password,
                                        pretty_game_id,
                                        metas_queue,
                                        done_flag,
                                        auth_info
                                    ),
                                )
                            )

                        for p in processes:
                            p.start()
                        for p in processes:
                            p.join()

                else:
                    print_and_log(
                        "%s does not support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

        log_file.close()

    if sys.argv[1] == "datastore_specific":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        class NexToken3DS:
            def __init__(self):
                self.host = None
                self.port = None
                self.pid = None
                self.password = None

        nex_token = NexToken3DS()
        nex_token.host = sys.argv[3]
        nex_token.port = sys.argv[4]
        nex_token.pid = sys.argv[5]
        nex_token.password = sys.argv[6]

        game_key = sys.argv[7]
        nex_version = int(sys.argv[8])

        pretty_game_id = sys.argv[9]

        async def does_search_work(client):
            store = datastore.DataStoreClient(client)
            return await search_works(store)

        s = settings.default()
        s.configure(game_key, nex_version)
        if await retry_if_rmc_error(
            does_search_work,
            s,
            nex_token.host,
            nex_token.port,
            str(nex_token.pid),
            nex_token.password,
        ):
            print_and_log("This game DOES support search", log_file)

            max_queryable = 100

            async def get_initial_data(client):
                store = datastore.DataStoreClient(client)

                param = datastore.DataStoreSearchParam()
                param.result_range.offset = 0
                param.result_range.size = 1
                param.result_option = 0xFF
                res = await store.search_object(param)

                last_data_id = None
                if len(res.result) > 0:
                    last_data_id = res.result[0].data_id
                else:
                    # Try timestamp method from 2012 as a backup
                    param = datastore.DataStoreSearchParam()
                    param.created_after = common.DateTime.fromtimestamp(1325401200)
                    param.result_range.size = 1
                    param.result_option = 0xFF
                    res = await store.search_object(param)

                    if len(res.result) > 0:
                        last_data_id = res.result[0].data_id

                if last_data_id is None or last_data_id > 900000:
                    last_data_id = 900000

                late_time = None
                late_data_id = None
                timestamp = int(time.time())
                while True:
                    # Try to find reasonable time going back, starting at current time
                    param = datastore.DataStoreSearchParam()
                    param.created_after = common.DateTime.fromtimestamp(timestamp)
                    param.result_range.size = 1
                    param.result_option = 0xFF
                    res = await store.search_object(param)

                    if len(res.result) > 0:
                        late_time = res.result[0].create_time
                        late_data_id = res.result[0].data_id
                        break
                    elif timestamp > 1325401200:
                        # Take off 1 month
                        timestamp -= 2629800
                    else:
                        # Otherwise timestamp is less than 2012, give up
                        break

                return (last_data_id, late_time, late_data_id)

            last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                get_initial_data,
                s,
                nex_token.host,
                nex_token.port,
                str(nex_token.pid),
                nex_token.password,
            )

            if last_data_id is not None:
                print_and_log(
                    "First data id %d Late time %s Late data ID %d"
                    % (last_data_id, str(late_time), late_data_id),
                    log_file,
                )

                num_metas_threads = 16
                num_download_threads = 16

                log_lock = Lock()
                metas_queue = Queue()
                done_flag = Value("i", False)
                num_metas_threads_done = Value("i", 0)

                processes = []
                for i in range(num_metas_threads):
                    processes.append(
                        Process(
                            target=get_datastore_metas,
                            args=(
                                log_lock,
                                game_key,
                                nex_version,
                                nex_token.host,
                                nex_token.port,
                                nex_token.pid,
                                nex_token.password,
                                pretty_game_id,
                                metas_queue,
                                done_flag,
                                i,
                                num_metas_threads,
                                max_queryable,
                                last_data_id,
                                late_data_id,
                                num_metas_threads_done,
                            ),
                        )
                    )
                for i in range(num_download_threads):
                    processes.append(
                        Process(
                            target=get_datastore_data,
                            args=(
                                log_lock,
                                game_key,
                                nex_version,
                                nex_token.host,
                                nex_token.port,
                                nex_token.pid,
                                nex_token.password,
                                pretty_game_id,
                                metas_queue,
                                done_flag,
                            ),
                        )
                    )

                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

        else:
            print_and_log(
                "%s does not support search" % game["name"].replace("\n", " "), log_file
            )

    if sys.argv[1] == "datastore_use_db_specific":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.commit()

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        class NexToken3DS:
            def __init__(self):
                self.host = None
                self.port = None
                self.pid = None
                self.password = None

        nex_token = NexToken3DS()
        nex_token.host = sys.argv[3]
        nex_token.port = sys.argv[4]
        nex_token.pid = sys.argv[5]
        nex_token.password = sys.argv[6]

        game_key = sys.argv[7]
        nex_version = int(sys.argv[8])

        pretty_game_id = sys.argv[9]

        async def does_search_work(client):
            store = datastore.DataStoreClient(client)
            return await search_works(store)

        s = settings.default()
        s.configure(game_key, nex_version)
        if await retry_if_rmc_error(
            does_search_work,
            s,
            nex_token.host,
            nex_token.port,
            str(nex_token.pid),
            nex_token.password,
        ):
            print_and_log("This game DOES support search", log_file)

            max_queryable = 100

            async def get_initial_data(client):
                store = datastore.DataStoreClient(client)

                param = datastore.DataStoreSearchParam()
                param.result_range.offset = 0
                param.result_range.size = 1
                param.result_option = 0xFF
                res = await store.search_object(param)

                last_data_id = None
                if len(res.result) > 0:
                    last_data_id = res.result[0].data_id
                else:
                    # Try timestamp method from 2012 as a backup
                    param = datastore.DataStoreSearchParam()
                    param.created_after = common.DateTime.fromtimestamp(1325401200)
                    param.result_range.size = 1
                    param.result_option = 0xFF
                    res = await store.search_object(param)

                    if len(res.result) > 0:
                        last_data_id = res.result[0].data_id

                if last_data_id is None or last_data_id > 900000:
                    last_data_id = 900000

                late_time = None
                late_data_id = None
                timestamp = int(time.time())
                while True:
                    # Try to find reasonable time going back, starting at current time
                    param = datastore.DataStoreSearchParam()
                    param.created_after = common.DateTime.fromtimestamp(timestamp)
                    param.result_range.size = 1
                    param.result_option = 0xFF
                    res = await store.search_object(param)

                    if len(res.result) > 0:
                        late_time = res.result[0].create_time
                        late_data_id = res.result[0].data_id
                        break
                    elif timestamp > 1325401200:
                        # Take off 1 month
                        timestamp -= 2629800
                    else:
                        # Otherwise timestamp is less than 2012, give up
                        break

                return (last_data_id, late_time, late_data_id)

            last_data_id, late_time, late_data_id = await retry_if_rmc_error(
                get_initial_data,
                s,
                nex_token.host,
                nex_token.port,
                str(nex_token.pid),
                nex_token.password,
            )

            if last_data_id is not None and late_data_id is not None:
                print_and_log(
                    "First data id %d Late time %s Late data ID %d"
                    % (last_data_id, str(late_time), late_data_id),
                    log_file,
                )

                num_download_threads = 16

                log_lock = Lock()
                metas_queue = Queue()
                done_flag = Value("i", True)
                num_metas_threads_done = Value("i", 0)

                # Get all data IDs to download
                entries = (
                    con.cursor()
                    .execute(
                        "SELECT datastore_meta.data_id, owner_id FROM datastore_meta LEFT JOIN datastore_data ON datastore_meta.data_id = datastore_data.data_id WHERE datastore_meta.game = ? AND size > 0 AND data IS NULL",
                        (pretty_game_id,),
                    )
                    .fetchall()
                )

                print_and_log("Done reading from DB", log_file)

                processes = []
                for i in range(num_download_threads):
                    processes.append(
                        Process(
                            target=get_datastore_data,
                            args=(
                                log_lock,
                                game_key,
                                nex_version,
                                nex_token.host,
                                nex_token.port,
                                nex_token.pid,
                                nex_token.password,
                                pretty_game_id,
                                metas_queue,
                                done_flag,
                            ),
                        )
                    )

                for p in processes:
                    p.start()

                while True:
                    metas_queue.put(
                        [(int(entry[0]), int(entry[1])) for entry in entries[:100]]
                    )
                    entries = entries[100:]

                    if len(entries) == 0:
                        break

                for p in processes:
                    p.join()

        else:
            print_and_log(
                "%s does not support search" % game["name"].replace("\n", " "), log_file
            )

        log_file.close()

    if sys.argv[1] == "check_overlap":
        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"]
        f.close()

        f = open("../find-nex-servers/nex3ds.json")
        nex_3ds_games = json.load(f)["games"]
        f.close()

        print(set([game["aid"] for game in nex_wiiu_games]).intersection(set([game["aid"] for game in nex_3ds_games])))

    if sys.argv[1] == "datastore_persistence":
        con = sqlite3.connect(DATASTORE_DB, timeout=3600)
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta (
        game TEXT,
        data_id INTEGER,
        owner_id TEXT,
        size INTEGER,
        name TEXT,
        data_type INTEGER,
        meta_binary BLOB,
        permission INTEGER,
        delete_permission INTEGER,
        create_time INTEGER,
        update_time INTEGER,
        period INTEGER,
        status INTEGER,
        referred_count INTEGER,
        refer_data_id INTEGER,
        flag INTEGER,
        referred_time INTEGER,
        expire_time INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_tag (
        game TEXT NOT NULL,
        data_id INTEGER,
        tag TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_meta_rating (
        game TEXT,
        data_id INTEGER,
        slot INTEGER,
        total_value INTEGER,
        count INTEGER,
        initial_value INTEGER
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_data (
        game TEXT,
        data_id INTEGER,
        error TEXT,
        url TEXT,
        data BLOB
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_permission_recipients (
        game TEXT,
        data_id INTEGER,
        is_delete INTEGER,
        recipient TEXT
    )"""
        )
        con.execute(
            """
    CREATE TABLE IF NOT EXISTS datastore_persistent (
        game TEXT,
        owner_id TEXT,
        persistence_id INTEGER,
        data_id INTEGER
    )"""
        )
        con.commit()

        f = open("../find-nex-servers/nexwiiu.json")
        nex_wiiu_games = json.load(f)["games"][int(sys.argv[3]) :]
        f.close()

        wiiu_games = requests.get("https://kinnay.github.io/data/wiiu.json").json()[
            "games"
        ]

        log_file = open(DATASTORE_LOG, "a", encoding="utf-8")

        for i, game in enumerate(nex_wiiu_games):
            if i == int(sys.argv[4]):
                print("Reached intended end")
                break

            # Check if nexds is loaded
            has_datastore = bool(
                [g for g in wiiu_games if g["aid"] == game["aid"]][0]["nexds"]
            )

            if has_datastore:
                print_and_log(
                    "%s (%d out of %d)"
                    % (
                        game["name"].replace("\n", " "),
                        i + int(sys.argv[3]),
                        len(nex_wiiu_games),
                    ),
                    log_file,
                )

                pretty_game_id = hex(game["aid"])[2:].upper().rjust(16, "0")

                nas = nnas.NNASClient()
                nas.set_device(DEVICE_ID, SERIAL_NUMBER, SYSTEM_VERSION)
                nas.set_title(game["aid"], game["av"])
                nas.set_locale(REGION_ID, COUNTRY_NAME, LANGUAGE)

                access_token = await nas.login(USERNAME, PASSWORD)

                nex_token = await nas.get_nex_token(access_token.token, game["id"])

                nex_version = (
                    game["nex"][0][0] * 10000
                    + game["nex"][0][1] * 100
                    + game["nex"][0][2]
                )

                """
                # Run everything in processes
                num_processes = 8
                range_size = int(pow(2, 32) / num_processes)

                found_queue = Queue()
                num_tested_queue = Queue()

                processes = [Process(target=range_test_category,
                    args=(game["key"], nex_version, nex_token.host, nex_token.port, str(nex_token.pid), nex_token.password, i * range_size, i * range_size + 1000, found_queue, num_tested_queue)) for i in range(num_processes)]
                # Queue for printing number tested and found categories
                processes.append(Process(target=print_categories, args=(num_processes, found_queue, num_tested_queue)))
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()

                continue
                """

                async def does_search_work(client):
                    store = datastore.DataStoreClient(client)
                    return await search_works(store)

                s = settings.default()
                s.configure(game["key"], nex_version)
                if await retry_if_rmc_error(
                    does_search_work,
                    s,
                    nex_token.host,
                    nex_token.port,
                    str(nex_token.pid),
                    nex_token.password,
                ):
                    print_and_log(
                        "%s DOES support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

                    num_download_threads = 16

                    log_lock = Lock()
                    pids_queue = Queue()

                    pids = (
                        con.cursor()
                        .execute(
                            "SELECT DISTINCT owner_id FROM datastore_meta WHERE game = ?",
                            (pretty_game_id,),
                        )
                        .fetchall()
                    )

                    print_and_log("Done reading from DB", log_file)

                    pids = [(int(entry[0]), i) for entry in pids for i in range(16)]
                    while True:
                        pids_queue.put(pids[:100])
                        pids = pids[100:]

                        if len(pids) == 0:
                            break

                    processes = []
                    for i in range(num_download_threads):
                        processes.append(
                            Process(
                                target=get_datastore_metas_pids,
                                args=(
                                    log_lock,
                                    game["key"],
                                    nex_version,
                                    nex_token.host,
                                    nex_token.port,
                                    nex_token.pid,
                                    nex_token.password,
                                    pretty_game_id,
                                    pids_queue,
                                    s,
                                ),
                            )
                        )

                    for p in processes:
                        p.start()
                    for p in processes:
                        p.join()

                else:
                    print_and_log(
                        "%s does not support search" % game["name"].replace("\n", " "),
                        log_file,
                    )

        log_file.close()


if __name__ == "__main__":
    if sys.platform == "linux" or sys.platform == "linux2":
        multiprocessing.set_start_method("spawn")
    anyio.run(main)
