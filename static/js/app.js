document.addEventListener('DOMContentLoaded', () => {
    const startButton = document.getElementById('start');
    const stopButton = document.getElementById('stop');
    const transcriptDiv = document.getElementById('transcript');
    const waveformCanvas = document.getElementById('waveform');
    const savePdfButton = document.getElementById('save-pdf');
    const summaryNote = document.getElementById('summary-note');
    let mediaRecorder;
    let audioChunks = [];
    let isRecording = false;
    let transcriptionInterval;
    let chunkInterval;
    let lastTranscription = ""; // 중복 방지용 마지막 텍스트

    // Canvas 설정
    const canvasCtx = waveformCanvas.getContext('2d');
    waveformCanvas.width = waveformCanvas.offsetWidth;
    waveformCanvas.height = waveformCanvas.offsetHeight;

    // AudioContext 및 AnalyserNode 설정
    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    let analyser;
    let source;

    function drawWaveform() {
        if (!analyser) return;

        requestAnimationFrame(drawWaveform);

        const bufferLength = analyser.fftSize;
        const dataArray = new Uint8Array(bufferLength);
        analyser.getByteTimeDomainData(dataArray);

        canvasCtx.clearRect(0, 0, waveformCanvas.width, waveformCanvas.height);
        canvasCtx.fillStyle = '#141619';
        canvasCtx.fillRect(0, 0, waveformCanvas.width, waveformCanvas.height);

        canvasCtx.lineWidth = 2;
        canvasCtx.strokeStyle = '#6c63ff';

        canvasCtx.beginPath();

        const sliceWidth = waveformCanvas.width * 1.0 / bufferLength;
        let x = 0;

        for (let i = 0; i < bufferLength; i++) {
            const v = dataArray[i] / 128.0;
            const y = (v * waveformCanvas.height) / 2;

            if (i === 0) {
                canvasCtx.moveTo(x, y);
            } else {
                canvasCtx.lineTo(x, y);
            }

            x += sliceWidth;
        }

        canvasCtx.lineTo(waveformCanvas.width, waveformCanvas.height / 2);
        canvasCtx.stroke();
    }

    if (startButton && stopButton && transcriptDiv) {
        startButton.addEventListener('click', async () => {
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];

                // MediaStream을 AudioContext에 연결
                source = audioContext.createMediaStreamSource(stream);
                analyser = audioContext.createAnalyser();
                analyser.fftSize = 2048;
                source.connect(analyser);

                drawWaveform();

                // Each chunk must be decodable on its own, so we restart the
                // recorder per slice rather than using a timeslice (which emits
                // headerless continuation chunks).
                mediaRecorder.addEventListener('dataavailable', (event) => {
                    if (event.data && event.data.size > 0) {
                        sendAudio(event.data);
                    }
                });

                mediaRecorder.start();
                chunkInterval = setInterval(() => {
                    if (mediaRecorder && mediaRecorder.state === 'recording') {
                        mediaRecorder.stop();
                        mediaRecorder.start();
                    }
                }, 5000);
                console.log("Recording started...");
                isRecording = true;

                startButton.disabled = true;
                stopButton.disabled = false;
                transcriptDiv.innerHTML = ""; // 녹음 시작 시 기존 텍스트 초기화

                // /transcribe returns text directly; polling would duplicate it.
            } catch (error) {
                console.error("Error accessing microphone:", error);
            }
        });

        stopButton.addEventListener('click', () => {
            if (mediaRecorder && mediaRecorder.state === "recording") {
                mediaRecorder.stop();
                console.log("Recording stopped.");
                isRecording = false;

                // 인터벌 클리어
                clearInterval(transcriptionInterval);
                clearInterval(chunkInterval);

                startButton.disabled = false;
                stopButton.disabled = true;

                // AudioContext 연결 해제
                if (source) source.disconnect();
                if (analyser) analyser.disconnect();
            }
        });
    }

    function sendAudio(audioBlob) {
        const formData = new FormData();
        const ext = (audioBlob.type.split('/')[1] || 'webm').split(';')[0];
        formData.append('file', audioBlob, `recording.${ext}`);

        console.log("Sending audio data to /transcribe");

        fetch('/transcribe', {
            method: 'POST',
            body: formData, // FormData를 body로 전송
        })
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`Server error: ${response.status} ${response.statusText}`);
                }
                return response.json();
            })
            .then((data) => {
                if (data.transcription) {
                    console.log("Transcription:", data.transcription);
                    updateTranscript(data.transcription);
                } else if (data.error) {
                    console.error("Transcription error:", data.error);
                }
            })
            .catch((error) => {
                console.error("Fetch error:", error);
            });
    }

    function fetchTranscription() {
        console.log("Fetching transcription...");
        fetch('/current_transcription')
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`Server error: ${response.status} ${response.statusText}`);
                }
                return response.json();
            })
            .then((data) => {
                if (data.transcription) {
                    updateTranscript(data.transcription);
                }
            })
            .catch((error) => {
                console.error("Error fetching transcription:", error);
            });
    }

    function updateTranscript(newText) {
        console.log("Updating transcript:", newText); 
        if (newText && newText !== lastTranscription) {
            lastTranscription = newText;
            transcriptDiv.textContent += newText + " ";
            transcriptDiv.scrollTop = transcriptDiv.scrollHeight; // 자동 스크롤
        }
    }
});