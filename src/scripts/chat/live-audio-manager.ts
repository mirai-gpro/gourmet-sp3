// src/scripts/chat/live-audio-manager.ts
/**
 * Gemini LiveAPI 用 AudioStreamManager
 *
 * 【設計原則】(仕様書02 セクション4.2)
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

// Expression フレームデータ型
export interface ExpressionFrame {
    values: number[];  // ARKit 52 ブレンドシェイプ値
}

export class LiveAudioManager {
    private audioContext: AudioContext | null = null;
    private mediaStream: MediaStream | null = null;
    private audioWorkletNode: AudioWorkletNode | null = null;
    private sourceNode: MediaStreamAudioSourceNode | null = null;
    private socket: any = null;

    public isAiSpeaking: boolean = false;
    private isStreaming: boolean = false;

    // PCM再生（24kHz）- 即時スケジューリング方式
    private nextPlayTime: number = 0;
    private scheduledSources: AudioBufferSourceNode[] = [];  // interrupt時にstop()用

    // ★ Expression同期機能（仕様書08 セクション4.1）
    private firstChunkStartTime: number = 0;          // 最初のチャンク再生時刻
    private expressionFrameBuffer: ExpressionFrame[] = [];  // フレームデータ
    public expressionFrameRate: number = 30;           // fps（デフォルト30）
    public expressionNames: string[] = [];             // ARKit ブレンドシェイプ名
    private _a2eDebugCounter: number = 0;              // デバッグログ間引き用

    // ========================================
    // セッション開始時に1度だけ呼ぶ
    // ========================================
    async initialize(socket: any): Promise<void> {
        if (this.audioContext) return; // 既に初期化済み

        this.socket = socket;

        // 1. AudioContext (1つだけ) - 48kHzでマイク入力
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
        const downsampleRatio = 48000 / 16000; // = 3
        const audioProcessorCode = `
        class LiveAudioProcessor extends AudioWorkletProcessor {
            constructor() {
                super();
                this.bufferSize = 4800; // 300ms分 at 16kHz
                this.buffer = new Int16Array(this.bufferSize);
                this.writeIndex = 0;
                this.ratio = ${downsampleRatio};
                this.inputSampleCount = 0;
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
                        if (this.writeIndex < this.bufferSize) {
                            const s = Math.max(-1, Math.min(1, channelData[i]));
                            const int16Value = s < 0 ? s * 0x8000 : s * 0x7FFF;
                            this.buffer[this.writeIndex++] = int16Value;
                        }
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

        const blob = new Blob([audioProcessorCode], { type: 'application/javascript' });
        const processorUrl = URL.createObjectURL(blob);
        await this.audioContext.audioWorklet.addModule(processorUrl);
        URL.revokeObjectURL(processorUrl);

        // 4. Node作成・接続
        this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
        this.audioWorkletNode = new AudioWorkletNode(this.audioContext, 'live-audio-processor');
        this.sourceNode.connect(this.audioWorkletNode);

        // 5. フラグによる送信制御
        this.audioWorkletNode.port.onmessage = (e) => {
            if (!this.isStreaming) return;
            if (this.isAiSpeaking) return; // 半二重: AI応答中は送信しない

            const audioChunk: Int16Array = e.data.audioChunk;
            const base64 = arrayBufferToBase64(audioChunk.buffer);
            this.socket.emit('live_audio_in', { data: base64 });
        };

        console.log('[LiveAudioManager] 初期化完了');
    }

    // ========================================
    // ストリーミング開始（live_ready後に呼ぶ）
    // ========================================
    startStreaming(): void {
        this.isStreaming = true;
        // AudioContextがsuspendedなら再開
        if (this.audioContext && this.audioContext.state === 'suspended') {
            this.audioContext.resume();
        }
        console.log('[LiveAudioManager] ストリーミング開始');
    }

    // ========================================
    // ストリーミング停止（マイクは切らない）
    // ========================================
    stopStreaming(): void {
        this.isStreaming = false;
        console.log('[LiveAudioManager] ストリーミング停止');
    }

    // ========================================
    // AI応答音声の再生（Web Audio API, iOS対策）
    // PCM 24kHz 16bit mono
    // ========================================
    playPcmAudio(pcmBase64: string): void {
        if (!this.audioContext) return;

        // ★ 最初のチャンク時にfirstChunkStartTimeを記録（仕様書08 セクション4.2）
        if (this.firstChunkStartTime === 0) {
            this.firstChunkStartTime = this.audioContext.currentTime;
        }

        const pcmBytes = base64ToArrayBuffer(pcmBase64);
        // PCM 24kHz 16bit mono → Float32
        const int16 = new Int16Array(pcmBytes);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 32768.0;
        }

        const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
        buffer.copyToChannel(float32, 0);

        // ★ 即時スケジューリング: チャンク到着時に未来時刻へ予約
        // onended待ちの隙間が発生しないため、ブツブツ切れを防止
        this._scheduleBuffer(buffer);
    }

    private _scheduleBuffer(buffer: AudioBuffer): void {
        if (!this.audioContext) return;

        const source = this.audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(this.audioContext.destination);

        const now = this.audioContext.currentTime;
        // 少なくとも now + 0.005s 後に再生開始（スケジューリングマージン）
        const startTime = Math.max(now + 0.005, this.nextPlayTime);
        source.start(startTime);
        this.nextPlayTime = startTime + buffer.duration;

        // interrupt用にソースを追跡、終了時に自動除去
        this.scheduledSources.push(source);
        source.onended = () => {
            const idx = this.scheduledSources.indexOf(source);
            if (idx !== -1) this.scheduledSources.splice(idx, 1);
        };
    }

    // ========================================
    // ★ Expression同期メソッド（仕様書08 セクション4.1, 4.3）
    // ========================================

    /**
     * 現在の再生オフセット（ms）を計算
     */
    getCurrentPlaybackOffset(): number {
        if (!this.audioContext || this.firstChunkStartTime === 0) return 0;
        return (this.audioContext.currentTime - this.firstChunkStartTime) * 1000;
    }

    /**
     * 現在のフレームインデックスからexpressionフレームを取得
     */
    getCurrentExpressionFrame(): ExpressionFrame | null {
        if (this.expressionFrameBuffer.length === 0) return null;

        // ★ 音声と同じ時間ベース（firstChunkStartTime）を使用
        // expressionフレームは音声の特定時点に対応するため、音声基準で正確に同期
        const offsetMs = this.getCurrentPlaybackOffset();
        const frameIndex = Math.floor((offsetMs / 1000) * this.expressionFrameRate);
        const clampedIndex = Math.min(frameIndex, this.expressionFrameBuffer.length - 1);

        if (clampedIndex < 0) return null;

        const frame = this.expressionFrameBuffer[clampedIndex];

        // デバッグ: 60フレームごと（約1秒）にログ出力
        this._a2eDebugCounter++;
        if (this._a2eDebugCounter % 60 === 0) {
            const jawOpenIdx = this.expressionNames.indexOf('jawOpen');
            const jawVal = jawOpenIdx >= 0 && frame.values[jawOpenIdx] !== undefined
                ? frame.values[jawOpenIdx].toFixed(3) : 'N/A';
            console.log(
                `[A2E Sync] offsetMs=${offsetMs.toFixed(0)}, frameIdx=${clampedIndex}/${this.expressionFrameBuffer.length}, jawOpen=${jawVal}`
            );
        }

        return frame;
    }

    /**
     * Socket.IO live_expression イベントデータをフレームバッファに追加
     */
    onExpressionReceived(data: {
        expressions: number[][];
        expression_names: string[];
        frame_rate: number;
        chunk_index: number;
    }): void {
        // フレームレートとブレンドシェイプ名を更新
        if (data.frame_rate) this.expressionFrameRate = data.frame_rate;
        if (data.expression_names && data.expression_names.length > 0) {
            this.expressionNames = data.expression_names;
        }

        // フレームデータをバッファに追加
        for (const values of data.expressions) {
            this.expressionFrameBuffer.push({ values });
        }

        // デバッグ: バッファ状態とjawOpen値を出力
        if (data.expressions.length > 0) {
            const jawOpenIdx = this.expressionNames.indexOf('jawOpen');
            const firstFrame = data.expressions[0];
            const lastFrame = data.expressions[data.expressions.length - 1];
            console.log(
                `[A2E Buffer] chunk=${data.chunk_index}, +${data.expressions.length}frames, total=${this.expressionFrameBuffer.length}, ` +
                `jawOpenIdx=${jawOpenIdx}, jawOpen=[${jawOpenIdx >= 0 ? firstFrame[jawOpenIdx]?.toFixed(3) : 'N/A'}..${jawOpenIdx >= 0 ? lastFrame[jawOpenIdx]?.toFixed(3) : 'N/A'}], ` +
                `firstChunkStartTime=${this.firstChunkStartTime.toFixed(3)}`
            );
        }
    }

    // ========================================
    // 再生キューをクリア（割り込み時）
    // ========================================
    clearPlaybackQueue(): void {
        this.nextPlayTime = 0;
        // スケジュール済みの全ソースを停止
        for (const source of this.scheduledSources) {
            try { source.stop(); } catch (_) { /* already stopped */ }
        }
        this.scheduledSources = [];
        // ★ expressionバッファもクリア
        this.expressionFrameBuffer = [];
        this.firstChunkStartTime = 0;
    }

    // ========================================
    // フラグ切り替え
    // ========================================
    onAiResponseStarted(): void {
        // ★ 新しいAI応答ターンの最初のチャンクのみリセット（仕様書08 セクション4.4）
        if (!this.isAiSpeaking) {
            this.firstChunkStartTime = 0;
            this.expressionFrameBuffer = [];
            this._a2eDebugCounter = 0;
        }
        this.isAiSpeaking = true;
    }

    onAiResponseEnded(): void {
        this.isAiSpeaking = false;
    }

    // ========================================
    // 完全終了時のみ全破棄
    // ========================================
    terminate(): void {
        this.isStreaming = false;
        this.clearPlaybackQueue();

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
        this.socket = null;
        console.log('[LiveAudioManager] 完全終了');
    }
}
