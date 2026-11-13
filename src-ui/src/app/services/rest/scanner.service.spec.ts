import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http'
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing'
import { TestBed } from '@angular/core/testing'
import { environment } from 'src/environments/environment'
import { ScannerService } from './scanner.service'

describe('ScannerService', () => {
  let httpTestingController: HttpTestingController
  let service: ScannerService

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
      ],
    })
    httpTestingController = TestBed.inject(HttpTestingController)
    service = TestBed.inject(ScannerService)
  })

  afterEach(() => httpTestingController.verify())

  it('calls the scanner endpoints', () => {
    service.listScanners().subscribe()
    httpTestingController
      .expectOne(`${environment.apiBaseUrl}scanners/`)
      .flush([])

    const options = {
      source: 'feeder' as const,
      color_mode: 'grayscale' as const,
      resolution: 300,
      paper_size: 'a4' as const,
    }
    service.startScan('abc', options).subscribe()
    const start = httpTestingController.expectOne(
      `${environment.apiBaseUrl}scanners/abc/scan/`
    )
    expect(start.request.method).toBe('POST')
    expect(start.request.body).toEqual(options)
    start.flush({})

    service.getJob('j1').subscribe()
    httpTestingController
      .expectOne(`${environment.apiBaseUrl}scan_jobs/j1/`)
      .flush({})

    service.getFile('j1').subscribe()
    const file = httpTestingController.expectOne(
      `${environment.apiBaseUrl}scan_jobs/j1/file/`
    )
    expect(file.request.responseType).toBe('blob')
    file.flush(new Blob())

    service.deleteJob('j1').subscribe()
    const del = httpTestingController.expectOne(
      `${environment.apiBaseUrl}scan_jobs/j1/`
    )
    expect(del.request.method).toBe('DELETE')
    del.flush(null)
  })
})
