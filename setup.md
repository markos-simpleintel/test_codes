# PBX Test Caller — Setup & Run Instructions

This project is an **automated test caller** for our Asterisk PBX. It dials the PBX,
plays pre-recorded audio prompts, sends keypad (DTMF) digits, listens for when the PBX
finishes speaking (turn-taking), and records each call for review.

| File | Tool | Use it for |
|------|------|------------|
| `pjsip_test_call.py` | Python + PJSUA2 | Functional tests — records audio, sends DTMF, smart turn-taking |
| `load_call.xml` | SIPp | High-volume load testing (many simultaneous calls) |

**Environment:** Ubuntu (native, or via WSL on Windows). The main dependency, `pjsua2`,
is compiled from source — this is reliable on Ubuntu and difficult on native Windows, so
Windows users should use WSL as described below.

> If a step fails, see the matching item in **[Troubleshooting](#troubleshooting)** (T1–T6).
> The notes there come from an actual run-through of these instructions.

---

## Step 1 — Get an Ubuntu terminal

**On Windows (recommended):** install WSL, which runs Ubuntu inside Windows.
In **PowerShell (Administrator)**:
```powershell
wsl --install -d Ubuntu
```
Restart the PC, open **Ubuntu** from the Start menu, and create a username and password
when prompted. (The password shows nothing as you type — that is normal.)
→ *If this fails with "Catastrophic failure" or "Class not registered", see **T1**.*

**On a Linux machine:** just open a terminal. Skip to Step 2.

You are ready when the prompt looks like `you@host:~$`.

---

## Step 2 — Install build tools
```bash
sudo apt update
sudo apt install -y build-essential python3-dev python3-pip python3-venv \
    swig libasound2-dev pkg-config wget
```
Confirm the compiler is actually installed before continuing:
```bash
gcc --version
```
You should see a real version (e.g. `gcc ... 15.2.0`).
→ *If `apt` showed network errors or `gcc` is "not found", see **T2**.*

---

## Step 3 — Build the `pjsua2` Python module

**3a. Download, configure, and compile** (this is the long part — several minutes):
```bash
cd ~
wget https://github.com/pjsip/pjproject/archive/refs/tags/2.14.1.tar.gz
tar xzf 2.14.1.tar.gz
cd pjproject-2.14.1

./configure CFLAGS="-fPIC -O2"
make dep && make

cd pjsip-apps/src/swig
make python
```
Notes: `./configure` printing many "checking ... no" lines is normal. `make dep && make`
takes several minutes. Each command should end with no `Error`.

**3b. Install the module so Python can find it.**
Run this block as-is (it auto-detects your Python version and the build folder, then copies
the module into Python's search path). This replaces `sudo make -C python install`, which
silently fails to copy on Python 3.12+ — see **T3** for why.
```bash
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
SITE="$HOME/.local/lib/python$PYVER/site-packages"
mkdir -p "$SITE"
BUILD=$(dirname "$(find "$HOME/pjproject-2.14.1" -name pjsua2.py -path '*build*lib.*' | head -1)")
cp "$BUILD"/pjsua2.py "$BUILD"/_pjsua2*.so "$SITE"/
echo "Copied to: $SITE"
```

---

## Step 4 — Verify the module imports
```bash
cd ~
python3 -c "import pjsua2; print('pjsua2 OK')"
```
You must see `pjsua2 OK` before continuing.
→ *If you get `ModuleNotFoundError: No module named 'pjsua2'`, see **T3**.*

---

## Step 5 — Install the Python helper dependency
```bash
python3 -m pip install --user --break-system-packages python-dotenv
```
Verify:
```bash
python3 -c "import dotenv; print('dotenv OK')"
```
The `--break-system-packages` flag is required on Python 3.12+ — see **T4** for why it is safe here.

---

## Step 6 — Get the project files and configure

**Bring the project into Ubuntu** (Windows files are visible under `/mnt/c/`):
```bash
cp -r "/mnt/c/Users/<your-windows-user>/OneDrive/Documents/test_codes" ~/test_codes
cd ~/test_codes
```

**Create a `.env` file** in that folder (same folder as `pjsip_test_call.py`) with the
correct PBX details. Create it with `nano .env` (paste the block below, then `Ctrl+O`,
`Enter` to save, `Ctrl+X` to exit). The filename is exactly `.env` — the leading dot makes
it hidden, so use `ls -a` to see it. Always run the script *from this folder*.
```ini
# --- PBX / network ---
ASTERISK_HOST=10.29.32.138      # IP of the Asterisk PBX
REMOTE_SIP_PORT=5060            # PBX SIP port
LOCAL_SIP_PORT=5062             # local SIP port for this test client
MEDIA_RTP_PORT=4000             # base RTP/audio port
MEDIA_RTP_PORT_RANGE=400        # RTP port range (4000–4400)

# --- Caller identity (must exist in the PBX) ---
CALLER_USER=1001                # SIP username
CALLER_PASS=                    # SIP password (fill in)
CALLER_DISPLAY=Rahul            # display name

# --- What to dial ---
DEST_NUMBER=19073750302         # number / service the PBX routes

# --- Test run settings ---
NUM_CALLS=1                     # how many calls to place (start with 1)
CALL_START_GAP_MS=200           # gap between calls
MAX_CALL_SECONDS=1800           # max call length (safety cap)

# --- Optional AMI ready events ---
USE_AMI_READY_EVENTS=0          # set 1 to wait for AMI instead of RTP silence
AMI_HOST=10.29.32.138           # Asterisk AMI host
AMI_PORT=5038                   # Asterisk AMI port
AMI_USER=                       # AMI username
AMI_SECRET=                     # AMI password
AMI_READY_EVENT_NAME=TestReadyForInput
AMI_EVENT_CALLER=               # optional caller/channel/uniqueid filter
AMI_TRACE_EVENTS=0              # set 1 to log AMI event summaries
```
Ask the PBX owner for the correct `CALLER_USER`, `CALLER_PASS`, and `DEST_NUMBER` —
they must match accounts in the PBX's `pjsip.conf`.

**Add the audio files.** The script plays these WAV files (format: 8000 Hz, mono,
16-bit PCM), which must sit in the project folder:
```
first.wav  name2.wav  birthday2.wav  yes.wav  no.wav
height.wav  weight.wav  palmer.wav  silence_60s.wav
```
Get these from whoever prepared the test. → *If a `.wav` is missing or wrong format, see **T5**.*

---

## Step 7 — Run the test
Run it **from the project folder** (the script looks for `.env` and the `.wav` files in the
current folder):
```bash
cd ~/test_codes
python3 pjsip_test_call.py
```
Expected log lines:
```
*** PJSUA2 STARTED ***
[call-01] starting direct INVITE call to sip:...
[call-01] media is ready
[call-01] starting playback: first.wav
[call-01] remote turn seems finished ...
```
The recording is saved to `call_recordings/mixed_01.wav`.
→ *If the call never connects or the recording is silent, see **T6**.*

---

## Step 8 — Verify the result

1. **Connected** — the log showed `media is ready` (not an error or busy signal).
2. **Flow ran** — the log shows each `starting playback: <file>` and the DTMF digits.
3. **Recording** — open `call_recordings/mixed_01.wav` and listen for:
   - Did the test caller wait for the PBX to finish each prompt before answering?
   - Did the PBX advance correctly after each answer and after the DTMF digits?
   - Any talk-over, cut-offs, or long awkward gaps?

This recording is the main deliverable: it shows whether the PBX (Asterisk + noise
suppression + Silero turn-taking) behaved correctly.

---

## Load testing with SIPp (Rahul's task)

For high-volume testing, the `load_call.xml` scenario is driven by SIPp:
```bash
sudo apt install -y sip-tester
sipp -sf load_call.xml -inf accounts.csv 10.29.32.138:5060 -m 100 -r 10
```
- `accounts.csv` supplies per-call display name / username / password.
- `-m 100` = total calls, `-r 10` = 10 new calls per second.
- Requires the `.ulaw` audio files referenced inside `load_call.xml`.

---

## Native Windows (only if WSL is not allowed)

Not recommended — `pjsua2` must be built with Visual Studio (C++ workload) + SWIG, by
opening `pjproject-vs14.sln`, building the `pjsua2` and `python` projects in Release/x64,
and copying `_pjsua2.pyd` and `pjsua2.py` next to `pjsip_test_call.py`. Follow the official
PJSIP Windows build docs.

---

## Troubleshooting

These are the issues actually encountered while running the steps above.

### T1 — `wsl --install` fails ("Catastrophic failure" / "Class not registered")
The WSL app is corrupted (a sign: `Get-AppxPackage *WindowsSubsystem*` shows
`SignatureKind : Developer` instead of `Store`). Fix, in **admin PowerShell**:
```powershell
# 1. Enable required Windows features, then RESTART
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

# 2. Remove the broken package, then RESTART
Get-AppxPackage *WindowsSubsystem* | Remove-AppxPackage
```
3. Download the official WSL `.msi` from https://github.com/microsoft/WSL/releases/latest
   (under "Assets"), run it, then: `wsl --install -d Ubuntu`.

### T2 — `apt` network errors, or `gcc` "command not found"
On a flaky network, `apt update`/`apt install` can print "Network is unreachable" /
"No route to host" and return to the prompt **without finishing the install**. Re-run the
Step 2 `apt install` line and confirm with `gcc --version` before continuing. Do not start
the build until `gcc --version` prints a real version.

### T3 — Why Step 3b copies the module manually (and `ModuleNotFoundError` if you skip it)
**Background:** `pjsua2` is two files — `pjsua2.py` (the Python part) and `_pjsua2*.so` (the
compiled C bridge). Python only imports modules that sit in one of its *search-path* folders.
The old `sudo make -C python install` was supposed to copy them there, but on Python 3.12+
(tested on 3.14) that copy is silently blocked by the "externally-managed environment" rule —
so the files stay only in the build folder, and `import pjsua2` fails with
`ModuleNotFoundError`. Step 3b avoids this by copying the two files directly into
`~/.local/lib/pythonX.Y/site-packages/` (a folder Python always searches, no sudo needed).

If you ever see `ModuleNotFoundError: No module named 'pjsua2'`, just re-run the Step 3b
block — it is safe to run again and will re-copy the files.

### T4 — Why Step 5 uses `--break-system-packages`
On Python 3.12+, Ubuntu blocks plain `pip install` to protect the system Python, raising
`externally-managed-environment`. The `--break-system-packages` flag overrides that. It is
safe here because we also pass `--user`, which installs into your personal folder
(`~/.local/...`) and never touches the system Python. `python-dotenv` is a tiny, pure-Python
library, so there is no risk to the OS.

### T5 — `could not play <file>.wav`
The audio file is missing or in the wrong format. All `.wav` files must be present in the
project folder and be **8000 Hz, mono, 16-bit PCM**.

### T6 — Call won't connect, or recording is silent
- **`401` / busy / declined:** wrong `CALLER_USER` / `CALLER_PASS` / `DEST_NUMBER`, or the
  account doesn't exist in the PBX.
- **No INVITE at all:** PBX unreachable — check `ASTERISK_HOST` and that SIP port 5060/UDP is open.
- **Connects but PBX hears silence:** RTP/NAT issue. Run the test from a Linux box on the
  **same network** as the PBX (not over WSL/VPN), and ensure RTP ports 4000–4400/UDP are open.

---

## Checklist before handing to a tester

- [ ] `python3 -c "import pjsua2"` prints `pjsua2 OK`
- [ ] `python-dotenv` installed
- [ ] `.env` filled in with correct PBX IP, user, password, dest number
- [ ] All required `.wav` files present (8000 Hz mono 16-bit)
- [ ] PBX is reachable on the network
- [ ] `python3 pjsip_test_call.py` produces a file in `call_recordings/`

---

## Client acceptance workflow (recommended)

The `pjsip_test_call.py` entry point enforces the requested order and creates evidence
under `test_results/`. Run all commands from the project directory in Ubuntu/WSL.

### 1. Prepare and verify (does not place a call)

```bash
cp .env.example .env
# Fill in the real SIP and AMI credentials, then add the WAV files to input_audios/.
python3 pjsip_test_call.py preflight
```

Do not continue until the output says `PRE-FLIGHT PASSED`. AMI access and transfer
detection are mandatory: they let the automation detect a GSR transfer path and hang up
before a live-agent bridge. Ask the PBX owner for the exact GSR trunk/channel token and
include it in the comma-separated `AMI_TRANSFER_DIAL_TARGETS` value.

### 2. Run the single sample

```bash
python3 pjsip_test_call.py single
```

The script captures caller, connected, and destination values from actual SIP/AMI events
and saves them in the single-test JSON report. It never invents or asks the tester to type
phone numbers. Review the `phone_identity_observations` section, compare it with the three
UIs, then send the observed values to IA and wait for confirmation. A detected GSR
transfer makes the test fail.

### 3. Run exactly two concurrent tests after IA confirms

```bash
python3 pjsip_test_call.py concurrent --ia-confirmed
```

Without `--ia-confirmed`, or without a passing single-test report containing actual
identity observations, the script refuses to start the concurrent calls.

### 4. Azure latency review

Give Nikita and Brandon the generated `*_concurrent_latency.csv`, the concurrent JSON
report, and the `.log` file from `test_results/`. The CSV uses UTC timestamps and elapsed
milliseconds for call start, connection, media-ready, playback, response, transfer, and
disconnect events, separated by call ID.

### 5. Progressive capacity testing after IA approval

Run one approved level at a time. Stop increasing when failures or unacceptable latency
appear:

```bash
python3 pjsip_test_call.py load --calls 5 --ia-confirmed
python3 pjsip_test_call.py load --calls 10 --ia-confirmed
python3 pjsip_test_call.py load --calls 15 --ia-confirmed
```

The accepted range is 2–100 calls. Each level receives separate evidence under
`test_results/`; a capacity limit must be based on those results, not an estimate.
