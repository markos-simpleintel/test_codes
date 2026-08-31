import socket

import pjsua2 as pj

from config import (
    ASTERISK_HOST,
    CALLER_DISPLAY,
    CALLER_PASS,
    CALLER_USER,
    FORCE_BIND_IP,
    FORCE_PUBLIC_IP,
    LOCAL_SIP_PORT,
    MAX_CALLS_HEADROOM,
    MEDIA_RTP_PORT,
    MEDIA_RTP_PORT_RANGE,
    MEDIA_THREAD_COUNT,
    MIN_RUNTIME_MAX_CALLS,
    NUM_CALLS,
    PJSIP_CONSOLE_LOG_LEVEL,
    PJSIP_FORCE_CONSOLE_LOG,
    PJSIP_LOG_LEVEL,
    REMOTE_SIP_PORT,
    TX_FRAME_PTIME_MS,
    USE_TCP,
)
from run_logging import log_error, log_info


def safe_set(obj, attr, value):
    if not hasattr(obj, attr):
        return False
    try:
        setattr(obj, attr, value)
        return True
    except Exception as exc:
        log_error(f"*** could not set {obj.__class__.__name__}.{attr}: {exc}")
        return False


def detect_local_ip_for_remote(remote_host: str, remote_port: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, remote_port))
        return sock.getsockname()[0]
    finally:
        sock.close()


def get_bind_ip() -> str:
    if FORCE_BIND_IP:
        return FORCE_BIND_IP
    return detect_local_ip_for_remote(ASTERISK_HOST, REMOTE_SIP_PORT)


def configure_codecs(ep: pj.Endpoint):
    try:
        codec_infos = ep.codecEnum2()
    except Exception as exc:
        log_error(f"*** codec enumeration failed: {exc}")
        return

    for codec in codec_infos:
        codec_id = codec.codecId
        priority = 255 if codec_id.startswith("PCMU/8000") else 0
        try:
            ep.codecSetPriority(codec_id, priority)
        except Exception as exc:
            log_error(f"*** failed to set codec priority for {codec_id}: {exc}")

    log_info("*** codec priority update done")


def make_transport(ep: pj.Endpoint, bind_ip: str) -> int:
    transport_config = pj.TransportConfig()
    transport_config.port = LOCAL_SIP_PORT
    safe_set(transport_config, "boundAddress", bind_ip)

    if FORCE_PUBLIC_IP:
        safe_set(transport_config, "publicAddress", FORCE_PUBLIC_IP)
        log_info(f"*** forcing public SIP address: {FORCE_PUBLIC_IP}")

    if USE_TCP:
        transport_type = pj.PJSIP_TRANSPORT_TCP
        log_info("*** using TCP transport")
    else:
        transport_type = pj.PJSIP_TRANSPORT_UDP
        log_info("*** using UDP transport")

    return ep.transportCreate(transport_type, transport_config)


def configure_endpoint(ep_config: pj.EpConfig):
    ep_config.logConfig.level = PJSIP_LOG_LEVEL

    console_level = PJSIP_CONSOLE_LOG_LEVEL
    if NUM_CALLS > 10 and console_level > 2 and not PJSIP_FORCE_CONSOLE_LOG:
        log_error(
            f"*** clamping PJSIP console log level {console_level} -> 2 for "
            f"{NUM_CALLS} concurrent calls (set PJSIP_FORCE_CONSOLE_LOG=1 "
            "to override)"
        )
        console_level = 2
    ep_config.logConfig.consoleLevel = console_level

    safe_set(ep_config.uaConfig, "userAgent", "")
    safe_set(ep_config.uaConfig, "natTypeInSdp", 0)
    safe_set(ep_config.uaConfig, "enableUpnp", False)

    requested_max_calls = max(NUM_CALLS + MAX_CALLS_HEADROOM, MIN_RUNTIME_MAX_CALLS)
    if safe_set(ep_config.uaConfig, "maxCalls", requested_max_calls):
        log_info(f"*** uaConfig.maxCalls = {requested_max_calls}")
    else:
        log_error("*** warning: could not set uaConfig.maxCalls")

    ep_config.medConfig.clockRate = 8000
    ep_config.medConfig.channelCount = 1
    ep_config.medConfig.sndClockRate = 8000
    safe_set(ep_config.medConfig, "audioFramePtime", TX_FRAME_PTIME_MS)
    ep_config.medConfig.quality = 4
    ep_config.medConfig.noVad = True
    ep_config.medConfig.sndAutoCloseTime = -1

    requested_media_ports = max(
        int(getattr(ep_config.medConfig, "maxMediaPorts", 0) or 0),
        NUM_CALLS * 4 + 64,
    )
    if safe_set(ep_config.medConfig, "maxMediaPorts", requested_media_ports):
        log_info(f"*** medConfig.maxMediaPorts = {requested_media_ports}")

    if safe_set(ep_config.medConfig, "threadCnt", MEDIA_THREAD_COUNT):
        log_info(f"*** medConfig.threadCnt = {MEDIA_THREAD_COUNT}")


def build_account_config(bind_ip: str, transport_id: int) -> pj.AccountConfig:
    account_config = pj.AccountConfig()
    account_config.idUri = (
        f'"{CALLER_DISPLAY}" <sip:{CALLER_USER}@{ASTERISK_HOST}>'
    )
    account_config.regConfig.registerOnAdd = False

    safe_set(account_config.sipConfig, "transportId", transport_id)
    account_config.sipConfig.authCreds.append(
        pj.AuthCredInfo("digest", "*", CALLER_USER, 0, CALLER_PASS)
    )
    safe_set(account_config.sipConfig, "authInitialEmpty", False)
    safe_set(account_config.sipConfig, "useSharedAuth", False)
    safe_set(
        account_config.sipConfig,
        "contactForced",
        f"sip:{CALLER_USER}@{bind_ip}:{LOCAL_SIP_PORT}",
    )
    safe_set(account_config.sipConfig, "contactParams", "")
    safe_set(account_config.sipConfig, "contactUriParams", "")

    safe_set(account_config.callConfig, "prackUse", pj.PJSUA_100REL_NOT_USED)
    safe_set(account_config.callConfig, "timerUse", pj.PJSUA_SIP_TIMER_INACTIVE)

    safe_set(account_config.natConfig, "contactRewriteUse", 0)
    safe_set(account_config.natConfig, "viaRewriteUse", 0)
    safe_set(account_config.natConfig, "sdpNatRewriteUse", 0)
    safe_set(account_config.natConfig, "sipOutboundUse", 0)
    safe_set(account_config.natConfig, "contactUseSrcPort", 0)
    safe_set(account_config.natConfig, "udpKaIntervalSec", 0)
    safe_set(account_config.natConfig, "iceEnabled", False)
    safe_set(account_config.natConfig, "turnEnabled", False)
    safe_set(account_config.natConfig, "iceNoRtcp", True)
    safe_set(account_config.natConfig, "iceAlwaysUpdate", False)

    safe_set(account_config.mediaConfig.transportConfig, "port", MEDIA_RTP_PORT)
    safe_set(
        account_config.mediaConfig.transportConfig,
        "portRange",
        MEDIA_RTP_PORT_RANGE,
    )
    safe_set(account_config.mediaConfig.transportConfig, "boundAddress", bind_ip)
    safe_set(account_config.mediaConfig, "lockCodecEnabled", False)
    safe_set(account_config.mediaConfig, "streamKaEnabled", False)
    safe_set(account_config.mediaConfig, "rtcpXrEnabled", False)
    safe_set(account_config.mediaConfig, "rtcpMuxEnabled", False)

    try:
        safe_set(account_config.mediaConfig.rtcpFbConfig, "dontUseAvpf", True)
    except Exception:
        pass

    return account_config


def is_active_audio_media(media_description) -> bool:
    if media_description.type != pj.PJMEDIA_TYPE_AUDIO:
        return False

    status = getattr(media_description, "status", None)
    active_status = getattr(pj, "PJSUA_CALL_MEDIA_ACTIVE", None)
    if status is not None and active_status is not None and status != active_status:
        return False
    return True
