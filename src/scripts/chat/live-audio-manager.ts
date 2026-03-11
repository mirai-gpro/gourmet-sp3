/**
 * Gemini LiveAPI 用 AudioStreamManager
 *
 * 【設計原則】(02_liveapi_migration_design.md セクション4.2)
 * - AudioContext/MediaStream/AudioWorkletNode はセッション中使い回し
 * - 半二重制御はフラグ（isAiSpeaking）で行う
 * - VADはGemini側に委譲
 * - AI音声再生もWeb Audio APIで行う（iOS対策）
 */

const b64chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  const len = bytes.byteLength;
  for (let i = 0; i < len; i += 3) {
    const c1 = bytes[i];
    const c2 = bytes[i + 1];
    const c3 = bytes[i + 2];
    const enc1 = c1 >> 2;
    const enc2 = ((c1 & 3) << 4) | (c2 >> 4);
    const enc3 = ((c2 & 15) << 2) | (c3 >> 6);
    const enc4 = c3 & 63;
    binary += b64chars[enc1] + b64chars[enc2];
    if (Number.isNaN(c2)) { binary += '=='; }
    else if (Number.isNaN(c3)) { binary += b64chars[enc3] + '='; }
    else { binary += b64chars[enc3] + b64chars[enc4]; }
  }
  return binary;
}

function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binaryString = atob(base64);
  const len = binaryString.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = binaryString.charCodeAt(i);
  }
  return bytes.buffer;
}

export class LiveAudioManager {
  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private audioWorkletNode: AudioWorkletNode | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private socket: any = null;

  public isAiSpeaking: boolean = false;
  private isInitialized: boolean = false;
  private isStreaming: boolean = false;

  // 再生キュー: PCMチャンクを順序付きで再生
  private playbackQueue: Float32Array[] = [];
  private isPlaying: boolean = false;
  private nextPlaybackTime: number = 0;

  // ========================================
  // セッション開始時に1度だけ呼ぶ
  // ========================================
  async initialize(socket: any): Promise<void> {
    if (this.isInitialized) return;

    this.socket = socket;

    // 1. AudioContext (1つだけ)
    // @ts-ignore
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    this.audioContext = new AudioContextClass({ sampleRate: 48000 });

    // 2. getUserMedia (1回だけ)
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      }
    });

    // 3. AudioWorklet登録 (1回だけ)
    // 48kHz → 16kHz ダウンサンプリング + Int16変換
    const nativeSampleRate = this.audioContext.sampleRate;
    const downsampleRatio = nativeSampleRate / 16000;

    const processorCode = `
    class LiveAudioProcessor extends AudioWorkletProcessor {
      constructor() {
        super();
        this.ratio = ${downsampleRatio};
        this.inputSampleCount = 0;
        this.bufferSize = 4000;
        this.buffer = new Int16Array(this.bufferSize);
        this.writeIndex = 0;
      }
      process(inputs, outputs, parameters) {
        const input = inputs[0];
        if (!input || input.length === 0) return true;
        const channelData = input[0];
        if (!channelData || channelData.length === 0) return true;
        for (let i = 0; i < channelData.length; i++) {
          this.inputSampleCount++;
          if (this.inputSampleCount >= this.ratio) {
            this.inputSampleCount -= this.ratio;
            const s = Math.max(-1, Math.min(1, channelData[i]));
            const int16Value = s < 0 ? s * 0x8000 : s * 0x7FFF;
            this.buffer[this.writeIndex++] = int16Value;
            if (this.writeIndex >= this.bufferSize) {
              this.flush();
            }
          }
        }
        return true;
      }
      flush() {
        if (this.writeIndex === 0) return;
        const chunk = this.buffer.slice(0, this.writeIndex);
        this.port.postMessage({ audioChunk: chunk }, [chunk.buffer]);
        this.buffer = new Int16Array(this.bufferSize);
        this.writeIndex = 0;
      }
    }
    registerProcessor('live-audio-processor', LiveAudioProcessor);
    `;

    const blob = new Blob([processorCode], { type: 'application/javascript' });
    const processorUrl = URL.createObjectURL(blob);
    await this.audioContext.audioWorklet.addModule(processorUrl);
    URL.revokeObjectURL(processorUrl);

    // 4. Node作成・接続
    this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.audioWorkletNode = new AudioWorkletNode(this.audioContext, 'live-audio-processor');
    this.sourceNode.connect(this.audioWorkletNode);
    // destination接続不要（録音のみ、ローカル再生しない）

    // 5. フラグによる送信制御
    this.audioWorkletNode.port.onmessage = (e) => {
      if (!this.isStreaming) return;
      if (this.isAiSpeaking) return; // 半二重: AI応答中は送信しない

      const audioChunk = e.data.audioChunk; // Int16Array
      const base64 = arrayBufferToBase64(audioChunk.buffer);
      this.socket.emit('live_audio_in', { data: base64 });
    };

    this.isInitialized = true;
    console.log('[LiveAudio] 初期化完了');
  }

  // ========================================
  // ストリーミング開始/停止
  // ========================================
  startStreaming(): void {
    if (!this.isInitialized) {
      console.warn('[LiveAudio] 未初期化');
      return;
    }

    // AudioContextがsuspendedならresume
    if (this.audioContext && this.audioContext.state === 'suspended') {
      this.audioContext.resume();
    }

    this.isStreaming = true;
    console.log('[LiveAudio] ストリーミング開始');
  }

  stopStreaming(): void {
    this.isStreaming = false;
    console.log('[LiveAudio] ストリーミング停止');
  }

  // ========================================
  // AI応答音声の再生（Web Audio API, iOS対策）
  // PCM 24kHz 16bit mono → AudioBuffer
  // ========================================
  playPcmAudio(pcmBase64: string): void {
    if (!this.audioContext) return;

    const pcmBytes = base64ToArrayBuffer(pcmBase64);
    // PCM 24kHz 16bit mono → Float32
    const int16 = new Int16Array(pcmBytes);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768.0;
    }

    // キューに追加
    this.playbackQueue.push(float32);

    // 再生開始
    if (!this.isPlaying) {
      this.processPlaybackQueue();
    }
  }

  private processPlaybackQueue(): void {
    if (!this.audioContext || this.playbackQueue.length === 0) {
      this.isPlaying = false;
      return;
    }

    this.isPlaying = true;
    const float32 = this.playbackQueue.shift()!;

    const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
    buffer.copyToChannel(float32, 0);

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioContext.destination);

    // スケジュール再生でギャップを防ぐ
    const currentTime = this.audioContext.currentTime;
    const startTime = Math.max(currentTime, this.nextPlaybackTime);
    source.start(startTime);
    this.nextPlaybackTime = startTime + buffer.duration;

    source.onended = () => {
      this.processPlaybackQueue();
    };
  }

  // ========================================
  // 再生停止（割り込み時）
  // ========================================
  stopPlayback(): void {
    this.playbackQueue = [];
    this.isPlaying = false;
    this.nextPlaybackTime = 0;
  }

  // ========================================
  // フラグ切り替え
  // ========================================
  onAiResponseStarted(): void {
    this.isAiSpeaking = true;
  }

  onAiResponseEnded(): void {
    this.isAiSpeaking = false;
  }

  // ========================================
  // AudioContext unlock（iOS対策）
  // ========================================
  unlockAudioContext(): void {
    if (this.audioContext && this.audioContext.state === 'suspended') {
      this.audioContext.resume();
    }
  }

  // ========================================
  // 完全終了時のみ全破棄
  // ========================================
  terminate(): void {
    this.isStreaming = false;
    this.stopPlayback();

    if (this.audioWorkletNode) {
      this.audioWorkletNode.port.onmessage = null;
      this.audioWorkletNode.disconnect();
      this.audioWorkletNode = null;
    }

    if (this.sourceNode) {
      this.sourceNode.disconnect();
      this.sourceNode = null;
    }

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach(t => t.stop());
      this.mediaStream = null;
    }

    if (this.audioContext) {
      this.audioContext.close();
      this.audioContext = null;
    }

    this.isInitialized = false;
    this.socket = null;
    console.log('[LiveAudio] 完全終了');
  }
}
