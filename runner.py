import gc
import sys
import time

import pjsua2 as pj

from ami_events import AmiReadyEvents
from audio_assets import build_recording_path, load_audio_assets
from call_session import MyAccount, MyCall
from config import (
    ACTIONS,
    AMI_EVENT_CALLER,
    AMI_HOST,
    AMI_PORT,
    AMI_READY_EVENT_NAME,
    AMI_SECRET,
    AMI_TRACE_EVENTS,
    AMI_USER,
    CALL_START_GAP_MS,
    DEST_URI,
    DTMF_METHOD,
    MAX_CALL_SECONDS,
    NUM_CALLS,
    PY_GIL_SWITCH_INTERVAL,
    USE_AMI_READY_EVENTS,
    validate_config,
)
from pjsip_helpers import (
    build_account_config,
    configure_codecs,
    configure_endpoint,
    get_bind_ip,
    make_transport,
)
from run_logging import log_error, log_info, setup_run_logging


sys.setswitchinterval(PY_GIL_SWITCH_INTERVAL)


def main():
    endpoint = pj.Endpoint()
    account = None
    calls = []
    audio_assets = None
    ami_ready_events = None

    try:
        validate_config()
        audio_assets = load_audio_assets(ACTIONS)

        if USE_AMI_READY_EVENTS:
            try:
                ami_ready_events = AmiReadyEvents(
                    host=AMI_HOST,
                    port=AMI_PORT,
                    username=AMI_USER,
                    secret=AMI_SECRET,
                    ready_event_name=AMI_READY_EVENT_NAME,
                    caller_filter=AMI_EVENT_CALLER,
                    trace=AMI_TRACE_EVENTS,
                )
                ami_ready_events.start()
                log_info(f"*** AMI ready-event listener started: {AMI_HOST}:{AMI_PORT}")
            except Exception as exc:
                ami_ready_events = None
                log_error(
                    f"*** AMI listener unavailable; using audio silence: {exc}"
                )

        bind_ip = get_bind_ip()
        log_info(f"*** chosen local bind IP: {bind_ip}")

        endpoint_config = pj.EpConfig()
        configure_endpoint(endpoint_config)

        endpoint.libCreate()
        endpoint.libInit(endpoint_config)
        endpoint.audDevManager().setNullDev()

        transport_id = make_transport(endpoint, bind_ip)
        endpoint.libStart()
        log_info("*** PJSUA2 STARTED ***")

        configure_codecs(endpoint)

        account_config = build_account_config(bind_ip, transport_id)
        account = MyAccount()
        account.create(account_config)

        log_info("*** account created without registration")
        log_info(
            f"*** starting {NUM_CALLS} direct INVITE call(s), "
            f"DTMF method={DTMF_METHOD}"
        )

        for call_id in range(1, NUM_CALLS + 1):
            call = MyCall(
                ep=endpoint,
                acc=account,
                call_id=call_id,
                dst_uri=DEST_URI,
                actions=ACTIONS,
                mixed_recording=build_recording_path("mixed", call_id),
                audio_assets=audio_assets,
                ami_ready_events=ami_ready_events,
            )
            calls.append(call)
            call.log(f"starting direct INVITE to {DEST_URI} [{call.test_call_id}]")
            call.start()

            if call_id < NUM_CALLS and CALL_START_GAP_MS > 0:
                time.sleep(CALL_START_GAP_MS / 1000.0)

        started_at = time.time()
        while time.time() - started_at < MAX_CALL_SECONDS:
            if all(call.disconnected for call in calls):
                break
            time.sleep(0.1)

        remaining_calls = [call for call in calls if not call.disconnected]
        if remaining_calls:
            log_error(
                f"*** max call time reached, hanging up "
                f"{len(remaining_calls)} call(s)"
            )
            for call in remaining_calls:
                call.safe_hangup()

            wait_started_at = time.time()
            while time.time() - wait_started_at < 3:
                if all(call.disconnected for call in calls):
                    break
                time.sleep(0.1)

    except pj.Error as exc:
        log_error(f"*** PJSUA2 error: {exc}")
    except KeyboardInterrupt:
        log_error("*** interrupted, shutting down")
    except Exception as exc:
        log_error(f"*** general error: {exc}")
    finally:
        if ami_ready_events is not None:
            ami_ready_events.stop()

        for call in calls:
            call._stop_evt.set()
            call._ami_ready_evt.set()
            call._playback_done_evt.set()

        for call in calls:
            if not call.disconnected:
                call.safe_hangup()

        wait_started_at = time.time()
        while time.time() - wait_started_at < 5:
            if all(call.disconnected for call in calls):
                break
            time.sleep(0.1)

        for call in calls:
            driver_thread = call._driver_thread
            if driver_thread is not None and driver_thread.is_alive():
                driver_thread.join(timeout=3.0)

            transfer_thread = call._transfer_thread
            if transfer_thread is not None and transfer_thread.is_alive():
                transfer_thread.join(timeout=2.0)

        for call in calls:
            call.release_pjsua2_ownership()

        calls.clear()
        account = None
        audio_assets = None
        gc.collect()

        try:
            endpoint.libDestroy()
        except Exception as exc:
            log_error(f"*** libDestroy warning: {exc}")


def run():
    with setup_run_logging() as run_log:
        if run_log.path:
            log_info(f"*** runner log file: {run_log.path}")
        main()


if __name__ == "__main__":
    run()
