import socket
import pjsua2 as pj

try:
    from .config import (
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
        MIN_RUNTIME_MAX_CALLS,
        NUM_CALLS,
        REMOTE_SIP_PORT,
        USE_TCP,
    )
except ImportError:
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
        MIN_RUNTIME_MAX_CALLS,
        NUM_CALLS,
        REMOTE_SIP_PORT,
        USE_TCP,
    )


def safe_set(obj, attr, value):
    if not hasattr(obj, attr):
        return False
    try:
        setattr(obj, attr, value)
        return True
    except Exception as e:
        print(f"*** could not set {obj.__class__.__name__}.{attr}: {e}")
        return False


def describe_pj_error(err):
    fields = [f"repr={err!r}"]
    for attr in ("status", "reason", "title", "srcFile", "srcLine"):
        try:
            value = getattr(err, attr)
        except Exception:
            continue
        if value not in (None, ""):
            fields.append(f"{attr}={value}")
    return " ".join(fields)


def describe_media(label, media):
    if media is None:
        return f"{label}: <None>"

    fields = [f"{label}: class={media.__class__.__name__}"]

    try:
        fields.append(f"port_id={media.getPortId()}")
    except Exception as e:
        fields.append(f"port_id=<error {e!r}>")

    try:
        info = media.getPortInfo()
        for attr in ("portId", "name", "clockRate", "channelCount", "samplesPerFrame"):
            try:
                value = getattr(info, attr)
            except Exception:
                continue
            if value not in (None, ""):
                fields.append(f"{attr}={value}")
    except Exception as e:
        fields.append(f"port_info=<error {e!r}>")

    return " ".join(fields)


def detect_local_ip_for_remote(remote_host: str, remote_port: int) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]
    finally:
        s.close()


def get_bind_ip() -> str:
    if FORCE_BIND_IP:
        return FORCE_BIND_IP
    return detect_local_ip_for_remote(ASTERISK_HOST, REMOTE_SIP_PORT)


def print_transport_info(ep: pj.Endpoint, transport_id: int):
    try:
        info = ep.transportGetInfo(transport_id)
        print(f"*** transport type: {info.typeName}")
        print(f"*** transport localAddress: {format_socket_addr(info.localAddress)}")
        print(f"*** transport localName: {format_socket_addr(info.localName)}")
        print(f"*** transport info: {info.info}")
    except Exception as e:
        print(f"*** could not fetch transport info: {e}")


def format_socket_addr(value):
    host = getattr(value, "host", None)
    port = getattr(value, "port", None)
    if host is not None and port is not None:
        return f"{host}:{port}"
    return str(value)


def configure_codecs(ep: pj.Endpoint):
    try:
        codec_infos = ep.codecEnum2()
    except Exception as e:
        print(f"*** codec enumeration failed: {e}")
        return

    keep_prefixes = ("PCMU/8000",)

    print("*** codec list before priority update:")
    for c in codec_infos:
        print(f"    {c.codecId}")

    for c in codec_infos:
        codec_id = c.codecId
        prio = 0
        if any(codec_id.startswith(prefix) for prefix in keep_prefixes):
            prio = 255

        try:
            ep.codecSetPriority(codec_id, prio)
        except Exception as e:
            print(f"*** failed to set codec priority for {codec_id}: {e}")

    print("*** codec priority update done")


def make_transport(ep: pj.Endpoint, bind_ip: str) -> int:
    tp_cfg = pj.TransportConfig()
    tp_cfg.port = LOCAL_SIP_PORT
    safe_set(tp_cfg, "boundAddress", bind_ip)

    if FORCE_PUBLIC_IP:
        safe_set(tp_cfg, "publicAddress", FORCE_PUBLIC_IP)
        print(f"*** forcing public SIP address: {FORCE_PUBLIC_IP}")
    else:
        print("*** no public SIP address override")

    if USE_TCP:
        tp_type = pj.PJSIP_TRANSPORT_TCP
        print("*** using TCP transport")
    else:
        tp_type = pj.PJSIP_TRANSPORT_UDP
        print("*** using UDP transport")

    return ep.transportCreate(tp_type, tp_cfg)


def configure_endpoint(ep_cfg: pj.EpConfig):
    ep_cfg.logConfig.level = 2
    ep_cfg.logConfig.consoleLevel = 2

    safe_set(ep_cfg.uaConfig, "userAgent", "")
    safe_set(ep_cfg.uaConfig, "natTypeInSdp", 0)
    safe_set(ep_cfg.uaConfig, "enableUpnp", False)

    # Added:
    # Default PJSUA maxCalls is 4 unless overridden.
    requested_max_calls = max(NUM_CALLS + MAX_CALLS_HEADROOM, MIN_RUNTIME_MAX_CALLS)
    if safe_set(ep_cfg.uaConfig, "maxCalls", requested_max_calls):
        print(f"*** uaConfig.maxCalls set to {requested_max_calls}")
    else:
        print("*** warning: could not set uaConfig.maxCalls")

    ep_cfg.medConfig.clockRate = 8000
    ep_cfg.medConfig.channelCount = 1
    ep_cfg.medConfig.sndClockRate = 8000
    ep_cfg.medConfig.quality = 4
    ep_cfg.medConfig.noVad = True
    ep_cfg.medConfig.sndAutoCloseTime = -1


def build_account_config(bind_ip: str, transport_id: int) -> pj.AccountConfig:
    acfg = pj.AccountConfig()

    acfg.idUri = f'"{CALLER_DISPLAY}" <sip:{CALLER_USER}@{ASTERISK_HOST}>'
    acfg.regConfig.registerOnAdd = False

    safe_set(acfg.sipConfig, "transportId", transport_id)

    acfg.sipConfig.authCreds.append(
        pj.AuthCredInfo("digest", "*", CALLER_USER, 0, CALLER_PASS)
    )

    safe_set(acfg.sipConfig, "authInitialEmpty", False)
    safe_set(acfg.sipConfig, "useSharedAuth", False)

    safe_set(acfg.sipConfig, "contactForced", f"sip:{CALLER_USER}@{bind_ip}:{LOCAL_SIP_PORT}")
    safe_set(acfg.sipConfig, "contactParams", "")
    safe_set(acfg.sipConfig, "contactUriParams", "")

    safe_set(acfg.callConfig, "prackUse", pj.PJSUA_100REL_NOT_USED)
    safe_set(acfg.callConfig, "timerUse", pj.PJSUA_SIP_TIMER_INACTIVE)

    safe_set(acfg.natConfig, "contactRewriteUse", 0)
    safe_set(acfg.natConfig, "viaRewriteUse", 0)
    safe_set(acfg.natConfig, "sdpNatRewriteUse", 0)
    safe_set(acfg.natConfig, "sipOutboundUse", 0)
    safe_set(acfg.natConfig, "contactUseSrcPort", 0)
    safe_set(acfg.natConfig, "udpKaIntervalSec", 0)

    safe_set(acfg.natConfig, "iceEnabled", False)
    safe_set(acfg.natConfig, "turnEnabled", False)
    safe_set(acfg.natConfig, "iceNoRtcp", True)
    safe_set(acfg.natConfig, "iceAlwaysUpdate", False)

    safe_set(acfg.mediaConfig.transportConfig, "port", MEDIA_RTP_PORT)
    safe_set(acfg.mediaConfig.transportConfig, "portRange", MEDIA_RTP_PORT_RANGE)
    safe_set(acfg.mediaConfig.transportConfig, "boundAddress", bind_ip)

    safe_set(acfg.mediaConfig, "lockCodecEnabled", False)
    safe_set(acfg.mediaConfig, "streamKaEnabled", False)
    safe_set(acfg.mediaConfig, "rtcpXrEnabled", False)
    safe_set(acfg.mediaConfig, "rtcpMuxEnabled", False)

    try:
        safe_set(acfg.mediaConfig.rtcpFbConfig, "dontUseAvpf", True)
    except Exception:
        pass

    return acfg


def is_active_audio_media(media_desc) -> bool:
    if media_desc.type != pj.PJMEDIA_TYPE_AUDIO:
        return False

    status = getattr(media_desc, "status", None)
    active_const = getattr(pj, "PJSUA_CALL_MEDIA_ACTIVE", None)

    if status is not None and active_const is not None and status != active_const:
        return False

    return True



