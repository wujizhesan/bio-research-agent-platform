import { useEffect, useRef } from 'react'
import { XCircle } from 'lucide-react'

const modalFocusableSelector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), iframe, [tabindex]:not([tabindex="-1"])'

export function ReportPreviewModal({ preview, onClose }: { preview: { url: string; filename: string }; onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
    closeButtonRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return

      const dialog = dialogRef.current
      if (!dialog) return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(modalFocusableSelector))
      const first = focusable[0]
      const last = focusable.at(-1)
      if (!first || !last) {
        event.preventDefault()
        dialog.focus()
        return
      }

      const activeElement = document.activeElement
      if (event.shiftKey && (activeElement === first || !dialog.contains(activeElement))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (activeElement === last || !dialog.contains(activeElement))) {
        event.preventDefault()
        first.focus()
      }
    }

    const handleFocusIn = (event: FocusEvent) => {
      const dialog = dialogRef.current
      if (dialog && event.target instanceof Node && !dialog.contains(event.target)) closeButtonRef.current?.focus()
    }

    document.addEventListener('keydown', handleKeyDown)
    document.addEventListener('focusin', handleFocusIn)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.removeEventListener('focusin', handleFocusIn)
      previouslyFocused?.focus()
    }
  }, [onClose])

  return <div ref={dialogRef} tabIndex={-1} className="fixed inset-0 z-50 flex items-center justify-center bg-[#02090a]/80 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="report-preview-title">
    <div className="flex h-[min(88vh,900px)] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-[#365c78] bg-[#0a1a1d] shadow-[0_24px_80px_rgba(0,0,0,.55)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-4">
        <div><h2 id="report-preview-title" className="eyebrow text-[#8faecb]">HTML 报告预览</h2><div className="mt-1 truncate font-mono text-xs text-[#c8e3dc]">{preview.filename}</div></div>
        <button ref={closeButtonRef} type="button" onClick={onClose} className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-2 text-xs text-[#b7d1c9] transition hover:border-[#ec9b87] hover:text-white"><XCircle size={14} />关闭预览</button>
      </div>
      <iframe tabIndex={0} title={`HTML report preview ${preview.filename}`} src={preview.url} className="min-h-0 flex-1 bg-white" />
      <div className="border-t border-white/10 px-5 py-3 text-[10px] leading-5 text-[#73928a]">报告来自当前任务 artifact，并通过同一鉴权接口读取；预览内容不改变原始文件。</div>
    </div>
  </div>
}
