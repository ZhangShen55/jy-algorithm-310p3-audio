## ADDED Requirements

### Requirement: Audio dB/SNR endpoint
The system SHALL expose `POST /audio/db_snr` as a multipart form endpoint that accepts an audio upload and a detection window size.

#### Scenario: Valid multipart request
- **WHEN** a client posts `audioFile` and positive integer `time_size`
- **THEN** the system returns HTTP 200 with an audio metrics response.

#### Scenario: Empty filename
- **WHEN** a client posts an `audioFile` whose filename is empty
- **THEN** the system rejects the request before decoding the file.

#### Scenario: Invalid window size
- **WHEN** a client posts `time_size` less than or equal to zero
- **THEN** the system rejects the request before decoding the file.

### Requirement: Portable audio decoding
The system SHALL decode supported uploaded audio into mono signed 16-bit PCM before metric analysis.

#### Scenario: WAV, MP3, or AAC upload
- **WHEN** a client uploads a WAV, MP3, or AAC file
- **THEN** the system uses the configured FFmpeg executable to produce a mono PCM WAV for analysis.

#### Scenario: Missing FFmpeg executable
- **WHEN** the configured FFmpeg executable cannot be found
- **THEN** the system returns a clear processing error and deletes any temporary files created for the request.

#### Scenario: Decode failure
- **WHEN** FFmpeg cannot decode the uploaded file
- **THEN** the system returns a clear processing error and does not return partial metrics.

### Requirement: Windowed metric calculation
The system SHALL split decoded audio into contiguous windows based on `time_size` seconds and calculate metrics for each window.

#### Scenario: Full and partial windows
- **WHEN** decoded audio duration is not an exact multiple of `time_size`
- **THEN** the system returns one result item for each full window and one final result item for the remaining partial window.

#### Scenario: Window boundaries
- **WHEN** a result item is returned
- **THEN** `bg` is the window start time in seconds and `ed` is the window end time in seconds as floats.

#### Scenario: Empty decoded audio
- **WHEN** decoding succeeds but produces no samples
- **THEN** the system returns a clear processing error instead of an empty successful result.

### Requirement: dBFS and volume metrics
The system SHALL report dBFS metrics and mapped volume metrics for each result window.

#### Scenario: dBFS fields
- **WHEN** a result window contains samples
- **THEN** `db` is the whole-window RMS dBFS, `max_db` is the maximum analysis-frame dBFS, and `min_db` is the minimum analysis-frame dBFS, each rounded to two decimal places.

#### Scenario: Volume fields
- **WHEN** dBFS values are calculated
- **THEN** `volume`, `max_volume`, and `min_volume` are integers from 0 to 100 derived from the configured dB floor to 0 dBFS scale.

#### Scenario: Silent audio
- **WHEN** a window contains silence or near-silence
- **THEN** reported dBFS values are floored at the configured dB floor and the corresponding volume is 0.

### Requirement: Estimated SNR metric
The system SHALL report an estimated SNR value for each result window using only the uploaded audio.

#### Scenario: Normal speech window
- **WHEN** a window contains varying frame energy
- **THEN** `snr` is calculated from the ratio between high-energy signal frames and low-energy noise-floor frames and rounded to two decimal places.

#### Scenario: No meaningful signal
- **WHEN** a window has no meaningful energy difference between signal and noise-floor frames
- **THEN** `snr` is returned as `0.0`.

### Requirement: Response contract
The system SHALL return the exact response fields required by the interface contract.

#### Scenario: Successful response shape
- **WHEN** audio metrics are calculated successfully
- **THEN** the response contains `result`, `task_id`, `process_time_ms`, and `timestamp`.

#### Scenario: Result item shape
- **WHEN** the response includes a result item
- **THEN** the item contains `bg`, `ed`, `max_db`, `db`, `min_db`, `snr`, `max_volume`, `volume`, and `min_volume`.

#### Scenario: Response field types
- **WHEN** the response is serialized
- **THEN** `result` is a list, `task_id` is a string, `process_time_ms` is an integer, `timestamp` is an integer, dB/SNR/time fields are floats, and volume fields are integers.

### Requirement: ASR isolation
The system SHALL keep audio quality detection independent from ASR transcription execution.

#### Scenario: Metrics request
- **WHEN** a client calls `/audio/db_snr`
- **THEN** the system does not invoke Paraformer, SenseVoice, or punctuation CLIs.

#### Scenario: Existing ASR request
- **WHEN** a client calls `/v1.1.8/seacraft_asr`
- **THEN** existing ASR engine selection, punctuation, queueing, and response behavior remain unchanged.

### Requirement: Temporary audio cleanup
The system SHALL delete temporary audio files created for audio quality detection after processing completes or fails.

#### Scenario: Successful metrics request
- **WHEN** an `/audio/db_snr` request completes successfully
- **THEN** the uploaded temporary audio file and decoded temporary WAV file are deleted before the request finishes.

#### Scenario: Failed metrics request
- **WHEN** an `/audio/db_snr` request fails during decode or metric analysis
- **THEN** the uploaded temporary audio file and any decoded temporary WAV file are deleted before the error response is returned.
