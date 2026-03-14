// src/scripts/chat/audio-sync-player.ts
/**
 * AudioSyncPlayer - AudioContext.currentTime ベースの同期再生プレイヤー
 *
 * 仕様書08 セクション2.2:
 * PCMチャンク非同期到着に対応するため、AudioContext.currentTime で
 * 再生オフセットを追跡する。
 *
 * LAM_gpro より移植・改修:
 * - 元コードは HTML <audio> 要素の currentTime で同期
 * - 改修後は LiveAudioManager の getCurrentPlaybackOffset() で同期
 */

import type { LiveAudioManager, ExpressionFrame } from './live-audio-manager';

export class AudioSyncPlayer {
    private liveAudioManager: LiveAudioManager | null = null;
    private animationFrameId: number | null = null;
    private isRunning: boolean = false;

    // コールバック: expression フレーム更新時に呼ばれる
    private onExpressionUpdate: ((frame: ExpressionFrame | null) => void) | null = null;

    /**
     * LiveAudioManager をバインド
     */
    bindLiveAudioManager(manager: LiveAudioManager): void {
        this.liveAudioManager = manager;
    }

    /**
     * expressionフレーム更新コールバックを設定
     */
    setExpressionCallback(callback: (frame: ExpressionFrame | null) => void): void {
        this.onExpressionUpdate = callback;
    }

    /**
     * 同期ループを開始
     * requestAnimationFrame で毎フレーム expression データを取得し、
     * コールバックに渡す
     */
    start(): void {
        if (this.isRunning) return;
        this.isRunning = true;
        this._syncLoop();
    }

    /**
     * 同期ループを停止
     */
    stop(): void {
        this.isRunning = false;
        if (this.animationFrameId !== null) {
            cancelAnimationFrame(this.animationFrameId);
            this.animationFrameId = null;
        }
    }

    /**
     * 同期ループ本体
     * AudioContext.currentTime ベースでフレームインデックスを決定
     */
    private _syncLoop(): void {
        if (!this.isRunning) return;

        if (this.liveAudioManager && this.onExpressionUpdate) {
            const frame = this.liveAudioManager.getCurrentExpressionFrame();
            this.onExpressionUpdate(frame);
        }

        this.animationFrameId = requestAnimationFrame(() => this._syncLoop());
    }

    /**
     * 現在の再生オフセット（ms）を取得
     */
    getCurrentPlaybackOffset(): number {
        if (!this.liveAudioManager) return 0;
        return this.liveAudioManager.getCurrentPlaybackOffset();
    }

    /**
     * クリーンアップ
     */
    dispose(): void {
        this.stop();
        this.liveAudioManager = null;
        this.onExpressionUpdate = null;
    }
}
