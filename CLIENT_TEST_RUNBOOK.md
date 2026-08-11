# Client Test Runbook

## Before the test

1. Use Ubuntu or WSL and complete the installation steps in `setup.md`.
2. Copy `.env.example` to `.env` and enter the real SIP, PBX, and AMI values.
3. Confirm `AMI_TRANSFER_DIAL_TARGETS` contains the exact GSR trunk/channel token.
4. Put all required 8 kHz mono PCM WAV files in `input_audios/`.
5. Run:

   ```bash
   python3 pjsip_test_call.py preflight
   ```

   Continue only when it prints `PRE-FLIGHT PASSED`.

## Test 1 — single automated test

1. Keep Orbitty, Jane, and Transfer visible.
2. Run:

   ```bash
   python3 pjsip_test_call.py single
   ```

3. Confirm the final result says `SINGLE TEST PASSED`.
4. Open the generated `*_single_report.json` and review
   `phone_identity_observations`. These are captured from real SIP/AMI events.
5. Compare the observations with the numbers displayed in Orbiti, Jane, and Transfer.
6. Send the verified numbers to IA and wait for confirmation.

The run fails if the call does not connect, AMI monitoring is unavailable, or a GSR
transfer is detected. When the transfer route is detected, the automation stops its
actions and hangs up before the agent bridge.

## Test 2 — exactly two concurrent automated tests

Only after IA confirms the three numbers, run:

```bash
python3 pjsip_test_call.py concurrent --ia-confirmed
```

The command refuses to start without a passing single-test report and the explicit IA
confirmation flag.

## Evidence to send for Azure latency review

From `test_results/`, send Nikita and Brandon:

- the latest `*_concurrent_report.json`;
- the matching `*_concurrent_latency.csv`;
- the matching `*_concurrent.log`.

The CSV contains UTC time, elapsed milliseconds, call ID, classified event, and the raw
message. This allows both concurrent calls to be compared independently.

## Approved capacity testing

Do not run this section until IA explicitly approves load testing. Test one level at a
time and stop increasing when a level is unstable:

```bash
python3 pjsip_test_call.py load --calls 5 --ia-confirmed
python3 pjsip_test_call.py load --calls 10 --ia-confirmed
python3 pjsip_test_call.py load --calls 15 --ia-confirmed
python3 pjsip_test_call.py load --calls 20 --ia-confirmed
```

Continue with 25, 30, 40, and 50 only while the preceding level is stable. Each command
creates its own JSON report, latency CSV, and log. Report only the highest level actually
demonstrated as stable; do not infer the system limit.

## Result interpretation

- `PASSED`: every requested call connected, AMI monitoring was active, and no configured
  GSR transfer path was detected.
- `FAILED`: inspect the JSON report and log before retrying.
- `BLOCKED`: a prerequisite or IA approval is missing; no concurrent call was placed.
