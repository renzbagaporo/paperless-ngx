import { HttpClient } from '@angular/common/http'
import { Injectable, inject } from '@angular/core'
import { Observable } from 'rxjs'
import { environment } from 'src/environments/environment'

export type ScanSourceId = 'flatbed' | 'feeder' | 'feeder_duplex'
export type ScanColorMode = 'color' | 'grayscale' | 'bw'
export type ScanPaperSize = 'a4' | 'letter' | 'legal' | 'a5'

export interface ScanSource {
  id: ScanSourceId
  color_modes: ScanColorMode[]
  resolutions: number[]
  max_width: number
  max_height: number
  formats: string[]
}

export interface Scanner {
  id: string
  name: string
  host: string
  available: boolean
  error: string | null
  sources: ScanSource[]
}

export interface ScanOptions {
  source: ScanSourceId
  color_mode: ScanColorMode
  resolution: number
  paper_size: ScanPaperSize
  max_sheets?: number | null
  continue_job?: string
}

export interface ScanJob {
  id: string
  status: 'scanning' | 'done' | 'failed'
  pages: number
  /** Increases with each page edit. */
  revision?: number
  error: string | null
}

export interface ScanPageOperation {
  page: number
  rotate: number
}

@Injectable({
  providedIn: 'root',
})
export class ScannerService {
  private http = inject(HttpClient)

  listScanners(): Observable<Scanner[]> {
    return this.http.get<Scanner[]>(`${environment.apiBaseUrl}scanners/`)
  }

  startScan(scannerId: string, options: ScanOptions): Observable<ScanJob> {
    return this.http.post<ScanJob>(
      `${environment.apiBaseUrl}scanners/${scannerId}/scan/`,
      options
    )
  }

  getJob(jobId: string): Observable<ScanJob> {
    return this.http.get<ScanJob>(
      `${environment.apiBaseUrl}scan_jobs/${jobId}/`
    )
  }

  getFileUrl(jobId: string): string {
    return `${environment.apiBaseUrl}scan_jobs/${jobId}/file/`
  }

  getFile(jobId: string): Observable<Blob> {
    return this.http.get(this.getFileUrl(jobId), { responseType: 'blob' })
  }

  editPages(jobId: string, pages: ScanPageOperation[]): Observable<ScanJob> {
    return this.http.post<ScanJob>(
      `${environment.apiBaseUrl}scan_jobs/${jobId}/pages/`,
      { pages }
    )
  }

  deleteJob(jobId: string): Observable<void> {
    return this.http.delete<void>(
      `${environment.apiBaseUrl}scan_jobs/${jobId}/`
    )
  }
}
