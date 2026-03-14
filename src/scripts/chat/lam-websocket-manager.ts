// src/scripts/chat/lam-websocket-manager.ts
/**
 * LAM WebSocket Manager - Gaussian Splat アバターの管理
 *
 * LAM_gpro より移植・改修:
 * - gaussian-splat-renderer-for-lam パッケージの公式APIを使用
 * - GaussianSplatRenderer.getInstance(div, assetPath, callbacks) パターン
 * - ARKit 52ブレンドシェイプのコールバック方式適用
 * - LiveAudioManager との同期連携
 *
 * 仕様書08 セクション6:
 * External TTS Player モードから LiveAudioManager モードへ変更
 */

import * as GaussianSplats3D from 'gaussian-splat-renderer-for-lam';
import type { ExpressionFrame } from './live-audio-manager';

export interface LAMConfig {
    containerElement: HTMLDivElement;   // レンダラーを埋め込むdiv要素
    modelUrl: string;                  // .zip モデルURL
}

// ARKit 52 ブレンドシェイプ名（標準順序）
export const ARKIT_BLENDSHAPE_NAMES = [
    'eyeBlinkLeft', 'eyeLookDownLeft', 'eyeLookInLeft', 'eyeLookOutLeft', 'eyeLookUpLeft',
    'eyeSquintLeft', 'eyeWideLeft', 'eyeBlinkRight', 'eyeLookDownRight', 'eyeLookInRight',
    'eyeLookOutRight', 'eyeLookUpRight', 'eyeSquintRight', 'eyeWideRight',
    'jawForward', 'jawLeft', 'jawRight', 'jawOpen',
    'mouthClose', 'mouthFunnel', 'mouthPucker', 'mouthLeft', 'mouthRight',
    'mouthSmileLeft', 'mouthSmileRight', 'mouthFrownLeft', 'mouthFrownRight',
    'mouthDimpleLeft', 'mouthDimpleRight', 'mouthStretchLeft', 'mouthStretchRight',
    'mouthRollLower', 'mouthRollUpper', 'mouthShrugLower', 'mouthShrugUpper',
    'mouthPressLeft', 'mouthPressRight', 'mouthLowerDownLeft', 'mouthLowerDownRight',
    'mouthUpperUpLeft', 'mouthUpperUpRight',
    'browDownLeft', 'browDownRight', 'browInnerUp', 'browOuterUpLeft', 'browOuterUpRight',
    'cheekPuff', 'cheekSquintLeft', 'cheekSquintRight',
    'noseSneerLeft', 'noseSneerRight',
    'tongueOut'
];

export class LAMWebSocketManager {
    private renderer: GaussianSplats3D.GaussianSplatRenderer | null = null;
    private isModelLoaded: boolean = false;

    // 現在適用中の expression フレーム
    private currentExpression: ExpressionFrame | null = null;

    // チャット状態（Idle / Listening / Thinking / Responding）
    private chatState: string = 'Idle';

    // ブレンドシェイプ名マッピング
    private expressionNames: string[] = ARKIT_BLENDSHAPE_NAMES;

    /**
     * 初期化: Gaussian Splat レンダラーをセットアップ
     * 公式API: GaussianSplatRenderer.getInstance(div, assetPath, callbacks)
     */
    async initialize(config: LAMConfig): Promise<void> {
        try {
            this.renderer = await GaussianSplats3D.GaussianSplatRenderer.getInstance(
                config.containerElement,
                config.modelUrl,
                {
                    getChatState: () => this.chatState,
                    getExpressionData: () => this._getExpressionData(),
                    backgroundColor: '0x000000',
                    alpha: 0.0,
                }
            );

            // カメラ位置を調整してアバターの顔サイズ・位置を制御
            // デフォルト: x=0, y=1.8, z=1
            if (this.renderer.viewer && this.renderer.viewer.camera) {
                const camera = this.renderer.viewer.camera;
                camera.position.z = 0.4;    // 近づけて顔を大きく
                camera.position.y = 1.78;   // 顔の下半分が見えるよう上げる
                camera.updateProjectionMatrix();
                console.log('[LAMWebSocketManager] カメラ位置調整: y=', camera.position.y, 'z=', camera.position.z);
            }

            this.isModelLoaded = true;
            console.log('[LAMWebSocketManager] モデルロード完了');
        } catch (error) {
            console.error('[LAMWebSocketManager] 初期化エラー:', error);
            this.isModelLoaded = false;
        }
    }

    /**
     * コールバック: 現在のexpressionデータをブレンドシェイプmapとして返す
     */
    private _getExpressionData(): Record<string, number> {
        const result: Record<string, number> = {};

        if (!this.currentExpression) {
            // 静止状態: 全て0
            for (const name of this.expressionNames) {
                result[name] = 0;
            }
            return result;
        }

        const values = this.currentExpression.values;
        for (let i = 0; i < Math.min(values.length, this.expressionNames.length); i++) {
            result[this.expressionNames[i]] = values[i];
        }

        return result;
    }

    /**
     * expression フレームを更新
     */
    updateExpression(frame: ExpressionFrame | null): void {
        this.currentExpression = frame;
    }

    /**
     * チャット状態を更新
     */
    setChatState(state: string): void {
        this.chatState = state;
    }

    /**
     * ブレンドシェイプ名リストを設定
     */
    setExpressionNames(names: string[]): void {
        if (names && names.length > 0) {
            this.expressionNames = names;
        }
    }

    /**
     * モデルがロード済みかどうか
     */
    isLoaded(): boolean {
        return this.isModelLoaded;
    }

    /**
     * expression バッファをクリア（リセット）
     */
    resetExpression(): void {
        this.currentExpression = null;
        this.chatState = 'Idle';
    }

    /**
     * クリーンアップ
     */
    dispose(): void {
        this.renderer = null;
        this.isModelLoaded = false;
        this.currentExpression = null;
    }
}
