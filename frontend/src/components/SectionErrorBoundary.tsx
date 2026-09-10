import { Component, type ErrorInfo, type ReactNode } from 'react'
import { createFrontendErrorEvent, type FrontendErrorEvent } from '../app/frontendObservability'

type SectionErrorBoundaryProps = {
  children: ReactNode
  title: string
  description: string
  actionLabel?: string
  onReset?: () => void
  resetKeys?: readonly unknown[]
  variant?: 'panel' | 'overlay'
  boundaryName?: string
  traceId?: string
  jobId?: string
  pluginId?: string
  onError?: (event: FrontendErrorEvent) => void
}

type SectionErrorBoundaryState = {
  hasError: boolean
}

function resetKeysChanged(previous: readonly unknown[] = [], current: readonly unknown[] = []) {
  return previous.length !== current.length || previous.some((value, index) => !Object.is(value, current[index]))
}

export class SectionErrorBoundary extends Component<SectionErrorBoundaryProps, SectionErrorBoundaryState> {
  state: SectionErrorBoundaryState = { hasError: false }

  static getDerivedStateFromError(): SectionErrorBoundaryState {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const event = createFrontendErrorEvent({
      boundaryName: this.props.boundaryName || this.props.title,
      traceId: this.props.traceId,
      jobId: this.props.jobId,
      pluginId: this.props.pluginId,
    }, error, info.componentStack || '')
    console.error(`[${event.boundary_name}]`, event)
    this.props.onError?.(event)
  }

  componentDidUpdate(previousProps: SectionErrorBoundaryProps) {
    if (this.state.hasError && resetKeysChanged(previousProps.resetKeys, this.props.resetKeys)) {
      this.setState({ hasError: false })
    }
  }

  private reset = () => {
    this.props.onReset?.()
    this.setState({ hasError: false })
  }

  render() {
    if (!this.state.hasError) return this.props.children

    const fallback = (
      <section role="alert" className="w-full rounded-2xl border border-[#75483d] bg-[#2b1a1b] px-5 py-6 text-[#f6d7cd] shadow-[0_20px_60px_rgba(0,0,0,.28)]">
        <div className="font-mono text-[10px] tracking-[0.16em] text-[#f0a994]">局部故障已隔离</div>
        <h2 className="mt-2 text-lg font-semibold">{this.props.title}</h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[#ddb9ae]">{this.props.description}</p>
        <button type="button" onClick={this.reset} className="mt-4 rounded-lg border border-[#a86858] px-3 py-2 text-xs font-semibold text-[#ffd9cc] transition hover:bg-[#4a2925] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f0a994]">
          {this.props.actionLabel || '重试此区域'}
        </button>
      </section>
    )

    if (this.props.variant === 'overlay') {
      return <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#02090a]/80 p-4 backdrop-blur-sm">{fallback}</div>
    }
    return fallback
  }
}
