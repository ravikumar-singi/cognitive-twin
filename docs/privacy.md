# Privacy, consent and regulation

This MVP builds the controls in from the start. It is not legal advice: confirm the current state of each regulation with counsel before any deployment.

## Neural data

- Raw frames exist only inside `EdgeProcessor.sense` and are deleted before it returns. `raw_bytes_exported` is audited in tests and must stay at zero.
- Only three features leave the device: attention, cognitive load and signal quality.
- Several jurisdictions now treat neural data as sensitive personal data (for example, recent laws in Colorado and California in the US). Opt-in, on-device processing and revocability are the defaults here for that reason.

## Consent policy (`consent.py`)

| Condition | Effect |
|---|---|
| No neural opt-in | Sensing off; the twin runs on answers alone |
| Age under 16 | Sensing off by default |
| Under 18 without guardian consent | Enrolment refused |
| Region `EU` | Affect inference off: the EU AI Act prohibits emotion recognition in education settings, and treats education AI as high-risk |

## Grading firewall

Neural features can change *how* someone is taught, but never *whether* they're credentialed. `Evidence` is built from answers only and raises an error if any neural field appears.

## Children's and student data

Deployments involving minors need COPPA and FERPA review in the US, and the DPDP Act in India, among others. The defaults above (sensing off under 16, guardian consent) are a starting point, not compliance.

## Pseudonymity

Credentials name the learner by a DID derived from a hash of their internal id. Names never appear on the ledger.
