// astro.config.mjs
import { defineConfig } from 'astro/config';
import AstroPWA from '@vite-pwa/astro';

export default defineConfig({
  output: 'static',
  build: {
    assets: 'assets'
  },
  server: {
    port: 4321,
    host: true
  },
  vite: {
    envPrefix: 'PUBLIC_',
    build: {
      charset: 'utf8'
    }
  },
  integrations: [
    AstroPWA({
      registerType: 'autoUpdate',
      // ▼▼▼ 重要: 生成されるファイル名をHTMLと一致させる ▼▼▼
      manifestFilename: 'manifest.webmanifest',
      // ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

      includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Gourmet SP',
        short_name: 'Gourmet',
        description: '美味しいグルメを探すためのアプリ',
        theme_color: '#ffffff',
        background_color: '#ffffff',
        display: 'standalone',
        scope: '/',
        start_url: '/',
        icons: [
          {
            src: 'pwa-192x192.png', // publicフォルダにこの画像があること！
            sizes: '192x192',
            type: 'image/png'
          },
          {
            src: 'pwa-512x512.png', // publicフォルダにこの画像があること！
            sizes: '512x512',
            type: 'image/png'
          }
        ]
      },
      workbox: {
        // ★★★ 修正箇所: '/404' から '/index.html' に変更 ★★★
        // これでファイル未検出エラーがなくなり、SWが正常起動します
        navigateFallback: '/index.html',
        globPatterns: ['**/*.{css,js,html,svg,png,ico,txt}']
      }
    })
  ]
});
