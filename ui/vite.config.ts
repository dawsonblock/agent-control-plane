import { defineConfig } from 'vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'

// https://vite.dev/config/
// v0.7.6: React Compiler enabled via reactCompilerPreset + @rolldown/plugin-babel.
// The compiler automatically memoizes component render output, eliminating the
// need for manual useCallback/useMemo in most cases.
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset({ target: '19' })] }),
  ],
})
