import { EventEmitter, signal } from '@angular/core'
import { ComponentFixture, TestBed } from '@angular/core/testing'
import { NgbActiveModal, NgbModal } from '@ng-bootstrap/ng-bootstrap'
import { NgxBootstrapIconsModule, allIcons } from 'ngx-bootstrap-icons'
import { of, throwError } from 'rxjs'
import {
  ScanJob,
  Scanner,
  ScannerService,
} from 'src/app/services/rest/scanner.service'
import { ScanDialogComponent } from './scan-dialog.component'

const FEEDER: Scanner = {
  id: 'feeder000001',
  name: 'EPSON DS-571W',
  host: 'airsane',
  available: true,
  error: null,
  sources: [
    {
      id: 'feeder',
      color_modes: ['color', 'grayscale'],
      resolutions: [150, 300, 600],
      max_width: 2550,
      max_height: 4650,
      pdf: true,
    },
    {
      id: 'feeder_duplex',
      color_modes: ['color'],
      resolutions: [200, 600],
      max_width: 2550,
      max_height: 4650,
      pdf: true,
    },
  ],
}

const FLATBED: Scanner = {
  id: 'flatbed00001',
  name: 'EPSON GT-S660',
  host: 'airsane',
  available: true,
  error: null,
  sources: [
    {
      id: 'flatbed',
      color_modes: ['color'],
      resolutions: [75, 150],
      max_width: 2550,
      max_height: 3508,
      pdf: false,
    },
  ],
}

const OFFLINE: Scanner = {
  id: 'offline00001',
  name: 'airsane',
  host: 'airsane',
  available: false,
  error: 'The scanner is not reachable.',
  sources: [],
}

function job(overrides: Partial<ScanJob>): ScanJob {
  return { id: 'j1', status: 'scanning', pages: 0, error: null, ...overrides }
}

describe('ScanDialogComponent', () => {
  let component: ScanDialogComponent
  let fixture: ComponentFixture<ScanDialogComponent>
  let scannerService: jest.Mocked<ScannerService>
  let activeModal: { close: jest.Mock; dismiss: jest.Mock }

  function create(scanners: Scanner[] = [FEEDER, FLATBED]) {
    scannerService.listScanners.mockReturnValue(of(scanners))
    fixture = TestBed.createComponent(ScanDialogComponent)
    component = fixture.componentInstance
    fixture.detectChanges()
  }

  afterEach(() => jest.useRealTimers())

  beforeEach(async () => {
    jest.useFakeTimers()
    localStorage.clear()
    scannerService = {
      listScanners: jest.fn(),
      startScan: jest.fn(),
      getJob: jest.fn(),
      getFile: jest.fn(),
      editPages: jest.fn(),
      getFileUrl: jest.fn((id: string) => `/api/scan_jobs/${id}/file/`),
      deleteJob: jest.fn().mockReturnValue(of(null)),
    } as any
    activeModal = { close: jest.fn(), dismiss: jest.fn() }
    await TestBed.configureTestingModule({
      imports: [ScanDialogComponent, NgxBootstrapIconsModule.pick(allIcons)],
      providers: [
        { provide: ScannerService, useValue: scannerService },
        { provide: NgbActiveModal, useValue: activeModal },
      ],
    }).compileComponents()
  })

  it('chooses sensible defaults and keeps them valid per source', () => {
    create()
    expect(component.scanner()).toBe(FEEDER)
    expect(component.source().id).toBe('feeder')
    expect(component.colorMode()).toBe('grayscale')
    expect(component.resolution()).toBe(300)
    expect(component.paperSize()).toBe('a4')

    component.selectSource(FEEDER.sources[1])
    expect(component.colorMode()).toBe('color')
    expect(component.resolution()).toBe(600)

    component.selectScanner(FLATBED)
    expect(component.source().id).toBe('flatbed')
    expect(component.resolution()).toBe(150)
  })

  it('remembers the last choices', () => {
    create()
    component.selectScanner(FLATBED)
    component.resolution.set(75)
    component.paperSize.set('letter')
    scannerService.startScan.mockReturnValue(of(job({})))
    scannerService.getJob.mockReturnValue(of(job({ status: 'done', pages: 1 })))
    component.scan()
    jest.advanceTimersByTime(1000)
    fixture.destroy()

    create()
    expect(component.scanner()).toBe(FLATBED)
    expect(component.resolution()).toBe(75)
    expect(component.paperSize()).toBe('letter')
  })

  it('lists unreachable scanners when none is available', () => {
    create([OFFLINE])
    expect(component.scanner()).toBeNull()
    expect(component.canScan()).toBe(false)
    expect(fixture.nativeElement.textContent).toContain(
      'The scanner is not reachable.'
    )
  })

  it('shows an error when scanners cannot be loaded', () => {
    scannerService.listScanners.mockReturnValue(
      throwError(() => ({ error: {} }))
    )
    fixture = TestBed.createComponent(ScanDialogComponent)
    component = fixture.componentInstance
    fixture.detectChanges()
    expect(component.error()).toBe('Could not load scanners.')
  })

  it('scans, adds more pages and finishes with a PDF file', () => {
    create()
    scannerService.startScan.mockReturnValue(of(job({ id: 'j1' })))
    scannerService.getJob
      .mockReturnValueOnce(of(job({ id: 'j1', pages: 1 })))
      .mockReturnValueOnce(of(job({ id: 'j1', status: 'done', pages: 2 })))
    component.scan()
    expect(scannerService.startScan).toHaveBeenCalledWith(FEEDER.id, {
      source: 'feeder',
      color_mode: 'grayscale',
      resolution: 300,
      paper_size: 'a4',
    })
    expect(component.scanning()).toBe(true)
    jest.advanceTimersByTime(1000)
    expect(component.job().pages).toBe(1)
    jest.advanceTimersByTime(1000)
    expect(component.scanning()).toBe(false)
    expect(component.finishedJob().pages).toBe(2)

    scannerService.startScan.mockReturnValue(of(job({ id: 'j2' })))
    scannerService.getJob.mockReturnValue(
      of(job({ id: 'j2', status: 'done', pages: 3 }))
    )
    component.scan()
    expect(scannerService.startScan).toHaveBeenLastCalledWith(
      FEEDER.id,
      expect.objectContaining({ continue_job: 'j1' })
    )
    jest.advanceTimersByTime(1000)
    expect(component.finishedJob().id).toBe('j2')

    scannerService.getFile.mockReturnValue(of(new Blob(['%PDF'])))
    component.finish()
    expect(scannerService.getFile).toHaveBeenCalledWith('j2')
    expect(scannerService.deleteJob).toHaveBeenCalledWith('j2')
    const file: File = activeModal.close.mock.calls[0][0]
    expect(file.type).toBe('application/pdf')
    expect(file.name).toMatch(/^Scan \d{4}-\d\d-\d\d \d\d-\d\d-\d\d\.pdf$/)
  })

  it('keeps earlier pages when a further scan fails', () => {
    create()
    component.finishedJob.set(job({ id: 'j1', status: 'done', pages: 2 }))
    scannerService.startScan.mockReturnValue(of(job({ id: 'j2' })))
    scannerService.getJob.mockReturnValue(
      of(job({ id: 'j2', status: 'failed', error: 'Paper jam.' }))
    )
    component.scan()
    jest.advanceTimersByTime(1000)
    expect(component.error()).toBe('Paper jam.')
    expect(component.finishedJob().id).toBe('j1')
    expect(scannerService.deleteJob).toHaveBeenCalledWith('j2')
  })

  it('shows the server error when a scan does not start', () => {
    create()
    scannerService.startScan.mockReturnValue(
      throwError(() => ({
        error: { error: 'Load the pages into the document feeder.' },
      }))
    )
    component.scan()
    expect(component.job()).toBeNull()
    expect(component.error()).toBe('Load the pages into the document feeder.')
  })

  it('reports a lost scan and a failed download', () => {
    create()
    scannerService.startScan.mockReturnValue(of(job({})))
    scannerService.getJob.mockReturnValue(throwError(() => ({ error: {} })))
    component.scan()
    jest.advanceTimersByTime(1000)
    expect(component.error()).toBe('The scan was lost.')

    component.finishedJob.set(job({ status: 'done', pages: 1 }))
    scannerService.getFile.mockReturnValue(throwError(() => ({ error: {} })))
    component.finish()
    expect(component.finishing()).toBe(false)
    expect(component.error()).toBe('The scanned file could not be downloaded.')
  })

  it('limits feeder scans to a number of sheets', () => {
    create()
    scannerService.startScan.mockReturnValue(of(job({})))
    scannerService.getJob.mockReturnValue(of(job({})))
    expect(component.isFeeder()).toBe(true)
    component.limitSheets.set(true)
    component.maxSheets.set(3)
    component.scan()
    expect(scannerService.startScan).toHaveBeenLastCalledWith(
      FEEDER.id,
      expect.objectContaining({ max_sheets: 3 })
    )
    component.job.set(null)

    component.maxSheets.set(null)
    expect(component.canScan()).toBe(false)

    component.selectScanner(FLATBED)
    expect(component.isFeeder()).toBe(false)
    expect(component.canScan()).toBe(true)
    component.scan()
    expect(scannerService.startScan.mock.lastCall[1]).not.toHaveProperty(
      'max_sheets'
    )
  })

  it('remembers the sheet limit', () => {
    create()
    component.limitSheets.set(true)
    component.maxSheets.set(2)
    scannerService.startScan.mockReturnValue(of(job({})))
    scannerService.getJob.mockReturnValue(of(job({})))
    component.scan()
    fixture.destroy()

    create()
    expect(component.limitSheets()).toBe(true)
    expect(component.maxSheets()).toBe(2)
  })

  it('previews the scanned pages', () => {
    create()
    expect(component.previewSrc()).toBeNull()
    scannerService.startScan.mockReturnValue(of(job({ id: 'j1' })))
    scannerService.getJob.mockReturnValue(
      of(job({ id: 'j1', status: 'done', pages: 3 }))
    )
    component.scan()
    jest.advanceTimersByTime(1000)
    expect(component.previewSrc()).toBe('/api/scan_jobs/j1/file/?revision=0')
    expect(component.previewPages()).toEqual([1, 2, 3])

    component.togglePreview(2)
    expect(component.previewPage()).toBe(2)
    component.togglePreview(2)
    expect(component.previewPage()).toBeNull()

    component.togglePreview(3)
    fixture.detectChanges()
    expect(
      fixture.nativeElement.querySelectorAll('pngx-pdf-viewer').length
    ).toBe(4)
  })

  it('edits the scanned pages in the PDF editor', () => {
    create()
    component.finishedJob.set(
      job({ id: 'j1', status: 'done', pages: 2, revision: 0 })
    )
    const editor = {
      title: '',
      btnCaption: '',
      sourceUrl: signal<string>(undefined),
      pagesOnly: signal(false),
      buttonsEnabled: signal(true),
      confirmClicked: new EventEmitter(),
      getOperations: () => [{ page: 2, rotate: 90, doc: 0 }],
    }
    const modal = { componentInstance: editor, close: jest.fn() }
    jest.spyOn(TestBed.inject(NgbModal), 'open').mockReturnValue(modal as any)

    component.editPages()
    expect(editor.sourceUrl()).toBe('/api/scan_jobs/j1/file/?revision=0')
    expect(editor.pagesOnly()).toBe(true)

    scannerService.editPages.mockReturnValue(
      of(job({ id: 'j1', status: 'done', pages: 1, revision: 1 }))
    )
    editor.confirmClicked.emit()
    expect(scannerService.editPages).toHaveBeenCalledWith('j1', [
      { page: 2, rotate: 90 },
    ])
    expect(modal.close).toHaveBeenCalled()
    expect(component.previewSrc()).toBe('/api/scan_jobs/j1/file/?revision=1')
    expect(component.previewPages()).toEqual([1])

    scannerService.editPages.mockReturnValue(
      throwError(() => ({ error: { error: 'Keep at least one page.' } }))
    )
    component.editPages()
    editor.confirmClicked.emit()
    expect(component.error()).toBe('Keep at least one page.')
  })

  it('discards server-side jobs on cancel', () => {
    create()
    component.job.set(job({ id: 'j2' }))
    component.finishedJob.set(job({ id: 'j1', status: 'done' }))
    component.cancel()
    expect(scannerService.deleteJob).toHaveBeenCalledWith('j1')
    expect(scannerService.deleteJob).toHaveBeenCalledWith('j2')
    expect(activeModal.dismiss).toHaveBeenCalled()
  })
})
