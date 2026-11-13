import {
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core'
import { FormsModule } from '@angular/forms'
import { NgbActiveModal, NgbModal } from '@ng-bootstrap/ng-bootstrap'
import { NgxBootstrapIconsModule } from 'ngx-bootstrap-icons'
import {
  Subject,
  Subscription,
  catchError,
  exhaustMap,
  filter,
  of,
  takeUntil,
  takeWhile,
  throwError,
  timer,
} from 'rxjs'
import {
  ScanColorMode,
  ScanJob,
  ScanOptions,
  ScanPaperSize,
  ScanSource,
  ScanSourceId,
  Scanner,
  ScannerService,
} from 'src/app/services/rest/scanner.service'
import { PDFEditorComponent } from '../pdf-editor/pdf-editor.component'
import { PngxPdfViewerComponent } from '../pdf-viewer/pdf-viewer.component'
import { PdfRenderMode } from '../pdf-viewer/pdf-viewer.types'

const OPTIONS_STORAGE_KEY = 'pngx-scan-options'
const POLL_INTERVAL_MS = 1000
const PREFERRED_RESOLUTION = 300

interface StoredOptions extends Partial<ScanOptions> {
  scanner?: string
  limit_sheets?: boolean
}

export const SOURCE_LABELS: Record<ScanSourceId, string> = {
  flatbed: $localize`Flatbed`,
  feeder: $localize`Document feeder, one side`,
  feeder_duplex: $localize`Document feeder, both sides`,
}

export const COLOR_MODE_LABELS: Record<ScanColorMode, string> = {
  color: $localize`Color`,
  grayscale: $localize`Grayscale`,
  bw: $localize`Black and white`,
}

export const PAPER_SIZE_LABELS: Record<ScanPaperSize, string> = {
  a4: 'A4',
  letter: $localize`Letter`,
  legal: $localize`Legal`,
  a5: 'A5',
}

/**
 * Scans from a network scanner and closes with the scanned PDF as a File.
 * Several scans can be combined before finishing, such as flatbed pages or
 * feeder batches.
 */
@Component({
  selector: 'pngx-scan-dialog',
  templateUrl: './scan-dialog.component.html',
  styleUrl: './scan-dialog.component.scss',
  imports: [FormsModule, NgxBootstrapIconsModule, PngxPdfViewerComponent],
})
export class ScanDialogComponent implements OnInit, OnDestroy {
  private activeModal = inject(NgbActiveModal)
  private scannerService = inject(ScannerService)
  private modalService = inject(NgbModal)

  PdfRenderMode = PdfRenderMode
  SOURCE_LABELS = SOURCE_LABELS
  COLOR_MODE_LABELS = COLOR_MODE_LABELS
  PAPER_SIZE_LABELS = PAPER_SIZE_LABELS
  paperSizes = Object.keys(PAPER_SIZE_LABELS) as ScanPaperSize[]

  title = $localize`Scan`
  finishCaption = $localize`Use scan`

  readonly loadingScanners = signal(true)
  readonly scanners = signal<Scanner[]>([])
  readonly scanner = signal<Scanner | null>(null)
  readonly source = signal<ScanSource | null>(null)
  readonly colorMode = signal<ScanColorMode | null>(null)
  readonly resolution = signal<number | null>(null)
  readonly paperSize = signal<ScanPaperSize>('a4')
  /** Feeder only: stop after maxSheets instead of emptying the feeder. */
  readonly limitSheets = signal(false)
  readonly maxSheets = signal(1)

  /** The scan in progress. */
  readonly job = signal<ScanJob | null>(null)
  /** The latest finished scan, holding every page scanned so far. */
  readonly finishedJob = signal<ScanJob | null>(null)
  readonly error = signal<string | null>(null)
  readonly finishing = signal(false)
  /** The page shown enlarged in the preview. */
  readonly previewPage = signal<number | null>(null)

  readonly isFeeder = computed(
    () => this.source()?.id.startsWith('feeder') ?? false
  )

  readonly previewSrc = computed(() => {
    const finished = this.finishedJob()
    if (!finished) return null
    // The revision makes the viewer reload the file after page edits.
    return `${this.scannerService.getFileUrl(finished.id)}?revision=${finished.revision ?? 0}`
  })

  readonly previewPages = computed(() =>
    Array.from({ length: this.finishedJob()?.pages ?? 0 }, (_, i) => i + 1)
  )

  private poll: Subscription | null = null
  private unsubscribe = new Subject<void>()
  /** Set once the dialog has let go of its server-side jobs. */
  private closed = false

  readonly scanning = computed(() => this.job()?.status === 'scanning')

  readonly availableScanners = computed(() =>
    this.scanners().filter((s) => s.available)
  )

  readonly canScan = computed(
    () =>
      !!this.scanner()?.available &&
      !!this.source() &&
      !!this.colorMode() &&
      !!this.resolution() &&
      (!this.isFeeder() || !this.limitSheets() || this.maxSheets() >= 1) &&
      !this.scanning() &&
      !this.finishing()
  )

  ngOnInit(): void {
    this.scannerService.listScanners().subscribe({
      next: (scanners) => {
        this.scanners.set(scanners)
        this.loadingScanners.set(false)
        this.applyStoredOptions()
      },
      error: (e) => {
        this.loadingScanners.set(false)
        this.error.set(
          this.errorMessage(e, $localize`Could not load scanners.`)
        )
      },
    })
  }

  ngOnDestroy(): void {
    this.discardJobs()
    this.unsubscribe.next()
    this.unsubscribe.complete()
  }

  private applyStoredOptions() {
    let stored: StoredOptions = {}
    try {
      stored = JSON.parse(localStorage.getItem(OPTIONS_STORAGE_KEY) ?? '{}')
    } catch {
      stored = {}
    }
    const scanners = this.availableScanners()
    this.selectScanner(
      scanners.find((s) => s.id === stored.scanner) ?? scanners[0] ?? null,
      stored
    )
  }

  private storeOptions() {
    try {
      localStorage.setItem(
        OPTIONS_STORAGE_KEY,
        JSON.stringify({
          scanner: this.scanner()?.id,
          source: this.source()?.id,
          color_mode: this.colorMode(),
          resolution: this.resolution(),
          paper_size: this.paperSize(),
          limit_sheets: this.limitSheets(),
          max_sheets: this.maxSheets(),
        })
      )
    } catch {
      // Remembering the choices is a convenience only.
    }
  }

  selectScanner(scanner: Scanner | null, preferred: StoredOptions = {}) {
    this.scanner.set(scanner)
    const sources = scanner?.sources ?? []
    this.selectSource(
      sources.find((s) => s.id === preferred.source) ?? sources[0] ?? null,
      preferred
    )
  }

  selectSource(source: ScanSource | null, preferred: StoredOptions = {}) {
    this.source.set(source)
    if (!source) {
      this.colorMode.set(null)
      this.resolution.set(null)
      return
    }
    const colorModes = source.color_modes
    this.colorMode.set(
      [preferred.color_mode, this.colorMode(), 'grayscale' as const].find(
        (m) => m && colorModes.includes(m)
      ) ??
        colorModes[0] ??
        null
    )

    const resolutions = source.resolutions
    this.resolution.set(
      [preferred.resolution, this.resolution()].find(
        (r) => r && resolutions.includes(r)
      ) ??
        resolutions.find((r) => r >= PREFERRED_RESOLUTION) ??
        resolutions[resolutions.length - 1] ??
        null
    )
    if (preferred.paper_size && this.paperSizes.includes(preferred.paper_size))
      this.paperSize.set(preferred.paper_size)
    if (preferred.limit_sheets !== undefined)
      this.limitSheets.set(!!preferred.limit_sheets)
    if (preferred.max_sheets >= 1) this.maxSheets.set(preferred.max_sheets)
  }

  scan() {
    if (!this.canScan()) return
    this.error.set(null)
    this.storeOptions()
    this.job.set({ id: null, status: 'scanning', pages: 0, error: null })
    const finished = this.finishedJob()
    this.scannerService
      .startScan(this.scanner().id, {
        source: this.source().id,
        color_mode: this.colorMode(),
        resolution: this.resolution(),
        paper_size: this.paperSize(),
        ...(this.isFeeder() && this.limitSheets()
          ? { max_sheets: this.maxSheets() }
          : {}),
        ...(finished ? { continue_job: finished.id } : {}),
      })
      .subscribe({
        next: (job) => {
          if (this.closed) {
            // The dialog closed while the scan was starting.
            this.scannerService.deleteJob(job.id).subscribe({ error: () => {} })
            return
          }
          this.job.set(job)
          this.pollJob(job.id)
        },
        error: (e) => {
          this.job.set(null)
          this.error.set(
            this.errorMessage(e, $localize`The scan did not start.`)
          )
        },
      })
  }

  private pollJob(jobId: string) {
    this.poll?.unsubscribe()
    this.poll = timer(POLL_INTERVAL_MS, POLL_INTERVAL_MS)
      .pipe(
        // Wait for a slow answer rather than cancelling it.
        exhaustMap(() =>
          this.scannerService.getJob(jobId).pipe(
            // The scan carries on at the server through a dropped request;
            // only a missing job ends the polling.
            catchError((e) =>
              e?.status === 404 ? throwError(() => e) : of(null)
            )
          )
        ),
        filter((job): job is ScanJob => job !== null),
        takeWhile((job) => job.status === 'scanning', true),
        takeUntil(this.unsubscribe)
      )
      .subscribe({
        next: (job) => {
          if (job.status === 'done') {
            this.finishedJob.set(job)
            this.previewPage.set(null)
            this.job.set(null)
          } else if (job.status === 'failed') {
            this.error.set(job.error)
            this.job.set(null)
            this.scannerService.deleteJob(jobId).subscribe({ error: () => {} })
          } else {
            this.job.set(job)
          }
        },
        error: (e) => {
          this.job.set(null)
          this.error.set(this.errorMessage(e, $localize`The scan was lost.`))
        },
      })
  }

  togglePreview(page: number) {
    this.previewPage.update((current) => (current === page ? null : page))
  }

  editPages() {
    const finished = this.finishedJob()
    if (!finished || this.scanning() || this.finishing()) return
    const modal = this.modalService.open(PDFEditorComponent, {
      backdrop: 'static',
      size: 'xl',
      scrollable: true,
    })
    const editor = modal.componentInstance as PDFEditorComponent
    editor.title = $localize`Edit scanned pages`
    editor.btnCaption = $localize`Apply`
    editor.sourceUrl.set(this.previewSrc())
    editor.pagesOnly.set(true)
    editor.confirmClicked.subscribe(() => {
      editor.buttonsEnabled.set(false)
      const pages = editor
        .getOperations()
        .map(({ page, rotate }) => ({ page, rotate }))
      this.scannerService.editPages(finished.id, pages).subscribe({
        next: (job) => {
          this.finishedJob.set(job)
          this.previewPage.set(null)
          this.error.set(null)
          modal.close()
        },
        error: (e) => {
          editor.buttonsEnabled.set(true)
          this.error.set(
            this.errorMessage(e, $localize`The page changes were not saved.`)
          )
          modal.close()
        },
      })
    })
  }

  finish() {
    const finished = this.finishedJob()
    if (!finished || this.scanning() || this.finishing()) return
    this.finishing.set(true)
    this.scannerService.getFile(finished.id).subscribe({
      next: (blob) => {
        this.discardJobs()
        this.activeModal.close(
          new File([blob], this.fileName(), { type: 'application/pdf' })
        )
      },
      error: (e) => {
        this.finishing.set(false)
        this.error.set(
          this.errorMessage(
            e,
            $localize`The scanned file could not be downloaded.`
          )
        )
      },
    })
  }

  cancel() {
    this.discardJobs()
    this.activeModal.dismiss()
  }

  /** Cancel a running scan and remove scanned pages from the server, once. */
  private discardJobs() {
    if (this.closed) return
    this.closed = true
    this.poll?.unsubscribe()
    for (const job of [this.job(), this.finishedJob()]) {
      if (job?.id)
        this.scannerService.deleteJob(job.id).subscribe({ error: () => {} })
    }
  }

  private fileName(): string {
    const now = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    return (
      `Scan ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
      `${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}.pdf`
    )
  }

  private errorMessage(e: any, fallback: string): string {
    return e?.error?.error ?? fallback
  }
}
