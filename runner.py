import gc
import time

import pjsua2 as pj

from ami_events import AmiReadyEvents
from call_session import MyAccount, MyCall
from config import (
    ACTIONS,
    AMI_DETECT_TRANSFER,
    AMI_EVENT_CALLER,
    AMI_HOST,
    AMI_PORT,
    AMI_READY_EVENT_NAME,
    AMI_SECRET,
    AMI_TRACE_EVENTS,
    AMI_TRANSFER_CONTEXT_PREFIX_LIST,
    AMI_TRANSFER_DIAL_TARGET_LIST,
    AMI_USE_AGI_STREAM_EVENTS,
    AMI_USER,
    CALL_START_GAP_MS,
    DEST_URI,
    HANGUP_ON_AMI_TRANSFER,
    MAX_CALL_SECONDS,
    NUM_CALLS,
    SILENCE_PAD_WAV,
    USE_AMI_READY_EVENTS,
    masked_secret,
)
from pjsip_helpers import (
    build_account_config,
    configure_codecs,
    configure_endpoint,
    get_bind_ip,
    make_transport,
    print_transport_info,
)


def main():
    ep = pj.Endpoint()
    acc = None
    calls = []
    ami_ready_events = None

    try:
        if USE_AMI_READY_EVENTS:
            print(
                "*** AMI config from .env: "
                f"host={AMI_HOST} port={AMI_PORT} user={AMI_USER or '<empty>'} "
                f"secret={masked_secret(AMI_SECRET)} "
                f"ready_event={AMI_READY_EVENT_NAME} "
                f"caller_filter={AMI_EVENT_CALLER or '<none>'} "
                f"trace={AMI_TRACE_EVENTS} "
                f"agi_stream_events={AMI_USE_AGI_STREAM_EVENTS} "
                f"detect_transfer={AMI_DETECT_TRANSFER} "
                f"transfer_context_prefixes={AMI_TRANSFER_CONTEXT_PREFIX_LIST} "
                f"transfer_dial_targets={AMI_TRANSFER_DIAL_TARGET_LIST} "
                f"hangup_on_transfer={HANGUP_ON_AMI_TRANSFER}"
            )
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
                print(
                    f"*** AMI ready-event listener started: {AMI_HOST}:{AMI_PORT} "
                    f"custom_event={AMI_READY_EVENT_NAME} "
                    f"agi_stream_events={AMI_USE_AGI_STREAM_EVENTS} "
                    f"detect_transfer={AMI_DETECT_TRANSFER}"
                )
            except Exception as e:
                ami_ready_events = None
                print(f"*** AMI listener unavailable; using RTP silence detection: {e}")

        bind_ip = get_bind_ip()
        print(f"*** chosen local bind IP: {bind_ip}")

        ep_cfg = pj.EpConfig()
        configure_endpoint(ep_cfg)

        ep.libCreate()
        ep.libInit(ep_cfg)
        ep.audDevManager().setNullDev()

        transport_id = make_transport(ep, bind_ip)
        ep.libStart()
        print("*** PJSUA2 STARTED ***")

        print_transport_info(ep, transport_id)
        configure_codecs(ep)

        acfg = build_account_config(bind_ip, transport_id)

        acc = MyAccount()
        acc.create(acfg)

        print("*** account created without registration")
        print(f"*** starting {NUM_CALLS} direct INVITE call(s)")

        for call_id in range(1, NUM_CALLS + 1):
            call = MyCall(
                ep=ep,
                acc=acc,
                call_id=call_id,
                dst_uri=DEST_URI,
                actions=ACTIONS,
                silence_wav=SILENCE_PAD_WAV,
                ami_ready_events=ami_ready_events,
            )
            calls.append(call)
            call.log(f"starting direct INVITE call to {DEST_URI}")
            call.start()

            if call_id < NUM_CALLS and CALL_START_GAP_MS > 0:
                time.sleep(CALL_START_GAP_MS / 1000.0)

        started = time.time()
        while time.time() - started < MAX_CALL_SECONDS:
            active_calls = [call for call in calls if not call.disconnected]
            if not active_calls:
                break
            time.sleep(0.1)

        remaining_calls = [call for call in calls if not call.disconnected]
        if remaining_calls:
            print(f"*** max call time reached, hanging up {len(remaining_calls)} remaining call(s)")
            for call in remaining_calls:
                call.safe_hangup()

            wait_start = time.time()
            while time.time() - wait_start < 3:
                if all(call.disconnected for call in calls):
                    break
                time.sleep(0.1)

    except pj.Error as e:
        print(f"*** PJSUA2 error: {e}")
    except KeyboardInterrupt:
        print("*** interrupted by user; hanging up active calls")
    except Exception as e:
        print(f"*** general error: {e}")
    finally:
        if ami_ready_events is not None:
            ami_ready_events.stop()

        for call in calls:
            if not call.disconnected:
                call.safe_hangup()

        wait_start = time.time()
        while time.time() - wait_start < 5:
            if all(c.disconnected for c in calls):
                break
            time.sleep(0.1)

        # Join wait threads so their bound-method references to MyCall are released
        # before calls.clear() drops the list reference. Without this, a running
        # thread keeps MyCall alive past libDestroy(), causing the C-level assertion
        # "call_id >= 0 && call_id < max_calls" when the destructor fires too late.
        for call in calls:
            t = call._wait_thread
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

            t = call._transfer_thread
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

        for call in calls:
            call.release_pjsua2_ownership()

        calls.clear()
        acc = None
        gc.collect()  # break any remaining reference cycles so pj.Call destructors fire now

        try:
            ep.libDestroy()
        except KeyboardInterrupt:
            print("*** libDestroy interrupted by user")
        except Exception as e:
            print(f"*** libDestroy warning: {e}")


if __name__ == "__main__":
    main()

