// src/scripts/chat/live-audio-manager.ts
/**
 * Gemini LiveAPI 用 AudioStreamManager
 *
 * 【設計原則】(仕様書02 セクション4.2)
 * - AudioContext/MediaStream/AudioWorkletNode はセッション中使い回し
 * - 半二重制御はフラグ（isAiSpeaking）で行う
 * - VADはGemini側に委譲
 * - AI音声再生もWeb Audio APIで行う（iOS対策）
 *
 * ★ フェーズ2拡張: Expression同期機能
 * - AudioContext.currentTime ベースの再生オフセット追跡
 * - expressionフレームバッファ管理
 * - LAMAvatarの描画ループから getCurrentExpressionFrame() で参照
 */

export interface ExpressionData {
    names: string[];
    weights: number[];
}

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
    private isStreaming: boolean = false;

    // PCM再生キュー（24kHz）
    private playbackQueue: AudioBuffer[] = [];
    private isPlaying: boolean = false;
    private nextPlayTime: number = 0;

    // ★ Expression同期用（AudioSyncPlayerから移植）
    private firstChunkStartTime: number = 0;
    private isFirstChunk: boolean = true;
    private expressionFrameBuffer: ExpressionData[] = [];
    private expressionFrameRate: number = 30;
    private expressionNames: string[] = [];

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

        const pcmBytes = base64ToArrayBuffer(pcmBase64);
        // PCM 24kHz 16bit mono → Float32
        const int16 = new Int16Array(pcmBytes);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 32768.0;
        }

        const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
        buffer.copyToChannel(float32, 0);

        // キューに追加してシーケンシャルに再生
        this.playbackQueue.push(buffer);
        this._processPlaybackQueue();
    }

    private _processPlaybackQueue(): void {
        if (this.isPlaying || this.playbackQueue.length === 0 || !this.audioContext) return;

        this.isPlaying = true;
        const buffer = this.playbackQueue.shift()!;

        const source = this.audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(this.audioContext.destination);

        const now = this.audioContext.currentTime;
        const startTime = Math.max(now, this.nextPlayTime);
        source.start(startTime);
        this.nextPlayTime = startTime + buffer.duration;

        // ★ 最初のチャンクの再生開始時刻を記録（Expression同期用）
        if (this.isFirstChunk) {
            this.firstChunkStartTime = startTime;
            this.isFirstChunk = false;
        }

        source.onended = () => {
            this.isPlaying = false;
            this._processPlaybackQueue();
        };
    }

    // ========================================
    // 再生キューをクリア（割り込み時）
    // ========================================
    clearPlaybackQueue(): void {
        this.playbackQueue = [];
        this.isPlaying = false;
        this.nextPlayTime = 0;
        // ★ Expression同期もリセット
        this._resetExpressionSync();
    }

    // ========================================
    // フラグ切り替え
    // ========================================
    onAiResponseStarted(): void {
        this.isAiSpeaking = true;
    }

    onAiResponseEnded(): void {
        this.isAiSpeaking = false;
        // ★ ターン終了時にExpression同期をリセット（次のターンに備える）
        this._resetExpressionSync();
    }

    // ========================================
    // ★ Expression同期機能（フェーズ2）
    // ========================================

    /**
     * 再生開始からの経過時間（ミリ秒）を取得
     * LAMAvatarの描画ループがexpressionフレームインデックス計算に使用
     */
    getCurrentPlaybackOffset(): number {
        if (!this.audioContext || this.firstChunkStartTime === 0) return 0;
        return (this.audioContext.currentTime - this.firstChunkStartTime) * 1000;
    }

    /**
     * 現在の再生位置に対応するexpressionフレームを取得
     * LAMAvatarの60fps描画ループから毎フレーム呼ばれる
     */
    getCurrentExpressionFrame(): ExpressionData | null {
        if (this.expressionFrameBuffer.length === 0) return null;
        if (!this.isAiSpeaking) return null;

        const offsetMs = this.getCurrentPlaybackOffset();
        const frameIndex = Math.floor((offsetMs / 1000) * this.expressionFrameRate);

        if (frameIndex < 0 || frameIndex >= this.expressionFrameBuffer.length) {
            return null;
        }

        return this.expressionFrameBuffer[frameIndex];
    }

    /**
     * バックエンドから受信したexpressionフレームをバッファに追加
     * core-controller.ts の 'live_expression' イベントハンドラから呼ばれる
     */
    onExpressionReceived(data: {
        chunk_index: number;
        names: string[];
        frames: { weights: number[] }[];
        frame_rate: number;
    }): void {
        this.expressionFrameRate = data.frame_rate || 30;
        this.expressionNames = data.names || [];

        for (const frame of data.frames) {
            this.expressionFrameBuffer.push({
                names: this.expressionNames,
                weights: frame.weights,
            });
        }

        console.log(`[LiveAudioManager] Expression受信: chunk=${data.chunk_index}, ` +
            `${data.frames.length}フレーム追加, 合計${this.expressionFrameBuffer.length}フレーム`);
    }

    /**
     * Expression同期状態をリセット（ターン完了・割り込み時）
     */
    private _resetExpressionSync(): void {
        this.firstChunkStartTime = 0;
        this.isFirstChunk = true;
        this.expressionFrameBuffer = [];
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
