// Default backend assumes the verifier runs locally; align protocol with the task pane
// to avoid mixed-content rejections inside the Word webview.
const inferredBackend = window.location.protocol === 'https:'
  ? 'https://localhost:8000'
  : 'http://localhost:8000';
const BACKEND_URL = import.meta.env.VITE_BACKEND_URL ?? inferredBackend;

interface ApiOccurrence {
  citation_category?: string | null;
  matched_text?: string | null;
  pin_cite?: string | null;
}

interface ApiCitation {
  resource_key: string;
  type?: string | null;
  status?: string | null;
  substatus?: string | null;
  normalized_citation?: string | null;
  occurrences?: ApiOccurrence[];
}

interface ApiResponse {
  citations?: ApiCitation[];
  extracted_text?: string | null;
}

let statusEl: HTMLElement | null = null;
let responseEl: HTMLElement | null = null;
let verifyButton: HTMLButtonElement | null = null;
let resultContainer: HTMLElement | null = null;
let summaryEl: HTMLElement | null = null;
let citationsEl: HTMLElement | null = null;
let textPreviewEl: HTMLElement | null = null;

function setStatus(message: string) {
  if (!statusEl) {
    statusEl = document.getElementById('status');
  }
  if (statusEl) {
    statusEl.textContent = message;
  }
}

function setResponse(content: unknown) {
  if (!responseEl) {
    responseEl = document.getElementById('response');
  }
  if (responseEl) {
    if (typeof content === 'string') {
      responseEl.textContent = content;
    } else {
      responseEl.textContent = JSON.stringify(content, null, 2);
    }
  }
}

function setTextPreview(content: string) {
  if (!textPreviewEl) {
    textPreviewEl = document.getElementById('text-preview');
  }
  if (textPreviewEl) {
    textPreviewEl.textContent = content;
  }
}

function resetResults() {
  if (!resultContainer) {
    resultContainer = document.getElementById('result-container');
  }
  if (!summaryEl) {
    summaryEl = document.getElementById('result-summary');
  }
  if (!citationsEl) {
    citationsEl = document.getElementById('citations');
  }

  if (summaryEl) {
    summaryEl.innerHTML = '';
  }

  if (citationsEl) {
    citationsEl.innerHTML = '';
  }

  setResponse('');
  setTextPreview('');

  if (resultContainer) {
    resultContainer.hidden = true;
  }
}

function renderStatusBadge(status: string | null | undefined) {
  const span = document.createElement('span');
  span.classList.add('status-badge');
  if (status) {
    const normalized = status.toLowerCase();
    if (normalized === 'verified') {
      span.classList.add('verified');
    } else if (normalized === 'warning') {
      span.classList.add('warning');
    } else if (normalized === 'error' || normalized === 'no_match') {
      span.classList.add('error');
    }
    span.textContent = status;
  } else {
    span.textContent = 'unknown';
  }
  return span;
}

function renderResults(payload: ApiResponse) {
  if (!resultContainer) {
    resultContainer = document.getElementById('result-container');
  }
  if (!summaryEl) {
    summaryEl = document.getElementById('result-summary');
  }
  if (!citationsEl) {
    citationsEl = document.getElementById('citations');
  }

  if (!resultContainer || !summaryEl || !citationsEl) {
    console.warn('Result container not found in DOM.');
    return;
  }

  const citations = payload.citations ?? [];
  const total = citations.length;
  const statusCounts = new Map<string, number>();

  for (const citation of citations) {
    const key = (citation.status ?? 'unknown').toLowerCase();
    statusCounts.set(key, (statusCounts.get(key) ?? 0) + 1);
  }

  summaryEl.innerHTML = '';

  const totalItem = document.createElement('li');
  totalItem.textContent = `Total citations: ${total}`;
  summaryEl.appendChild(totalItem);

  if (statusCounts.size > 0) {
    for (const [status, count] of statusCounts.entries()) {
      const li = document.createElement('li');
      const label = status.replace(/_/g, ' ');
      const prettyLabel = label.charAt(0).toUpperCase() + label.slice(1);
      li.textContent = `${prettyLabel}: ${count}`;
      summaryEl.appendChild(li);
    }
  }

  citationsEl.innerHTML = '';

  if (total === 0) {
    const emptyState = document.createElement('p');
    emptyState.textContent = 'No citations returned.';
    citationsEl.appendChild(emptyState);
  } else {
    const toDisplay = citations.slice(0, 5);
    toDisplay.forEach((citation) => {
      const card = document.createElement('article');
      card.classList.add('citation-card');

      const header = document.createElement('div');
      header.classList.add('citation-header');

      const title = document.createElement('h3');
      title.textContent = citation.normalized_citation ?? citation.resource_key;

      const badge = renderStatusBadge(citation.status);

      header.appendChild(title);
      header.appendChild(badge);
      card.appendChild(header);

      if (citation.type) {
        const type = document.createElement('p');
        type.textContent = `Type: ${citation.type}`;
        card.appendChild(type);
      }

      if (citation.substatus) {
        const sub = document.createElement('p');
        sub.textContent = `Detail: ${citation.substatus}`;
        card.appendChild(sub);
      }

      const firstOccurrence = citation.occurrences?.[0];
      if (firstOccurrence?.matched_text) {
        const snippet = document.createElement('p');
        snippet.textContent = `Example: ${firstOccurrence.matched_text}`;
        card.appendChild(snippet);
      }

      const occurrenceCount = citation.occurrences?.length ?? 0;
      if (occurrenceCount > 1) {
        const occurrenceInfo = document.createElement('p');
        occurrenceInfo.textContent = `${occurrenceCount} occurrences detected.`;
        card.appendChild(occurrenceInfo);
      }

      citationsEl.appendChild(card);
    });

    if (citations.length > toDisplay.length) {
      const remainder = document.createElement('p');
      remainder.textContent = `${citations.length - toDisplay.length} more citation(s) available in the raw response below.`;
      citationsEl.appendChild(remainder);
    }
  }

  setResponse(payload);

  const preview = payload.extracted_text ? payload.extracted_text.slice(0, 500) : 'No preview available.';
  setTextPreview(preview);

  resultContainer.hidden = false;
}

function decodeSlice(data: unknown): Uint8Array {
  if (typeof data === 'string') {
    const binary = atob(data);
    const buffer = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      buffer[i] = binary.charCodeAt(i);
    }
    return buffer;
  }

  if (data == null) {
    throw new Error('Received empty slice data.');
  }

  if (data instanceof Uint8Array) {
    return data;
  }

  if (data instanceof ArrayBuffer) {
    return new Uint8Array(data);
  }

  if (ArrayBuffer.isView(data)) {
    const view = data as ArrayBufferView;
    return new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
  }

  if (Array.isArray(data)) {
    return new Uint8Array(data as number[]);
  }

  const dataType = Object.prototype.toString.call(data);
  throw new Error(`Unsupported slice data format: ${dataType}`);
}

function mergeSlices(slices: Uint8Array[]): Uint8Array {
  const totalLength = slices.reduce((sum, slice) => sum + slice.byteLength, 0);
  const merged = new Uint8Array(totalLength);

  let offset = 0;
  for (const slice of slices) {
    merged.set(slice, offset);
    offset += slice.byteLength;
  }

  return merged;
}

async function getDocumentBytes(): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    Office.context.document.getFileAsync(
      Office.FileType.Compressed,
      { sliceSize: 65536 },
      (fileResult) => {
        if (fileResult.status !== Office.AsyncResultStatus.Succeeded || !fileResult.value) {
          reject(new Error(fileResult.error?.message ?? 'Unable to read document.'));
          return;
        }
        const file = fileResult.value;
        const sliceCount = file.sliceCount;
        const slices: (Uint8Array | undefined)[] = new Array(sliceCount);
        let slicesReceived = 0;

        const finalize = () => {
          if (slicesReceived === sliceCount) {
            try {
              const orderedSlices = slices.map((slice, index) => {
                if (!slice) {
                  throw new Error(`Missing slice ${index}`);
                }
                return slice;
              });

              const merged = mergeSlices(orderedSlices);
              file.closeAsync();
              resolve(merged);
            } catch (error) {
              file.closeAsync();
              reject(error instanceof Error ? error : new Error(String(error)));
            }
          }
        };

        const getSlice = (idx: number) => {
          file.getSliceAsync(idx, (sliceResult) => {
            if (sliceResult.status !== Office.AsyncResultStatus.Succeeded || !sliceResult.value) {
              file.closeAsync();
              reject(new Error(sliceResult.error?.message ?? 'Unable to read document slice.'));
              return;
            }

            try {
              const sliceData = decodeSlice(sliceResult.value.data);
              slices[idx] = sliceData;
              slicesReceived += 1;
              finalize();
            } catch (error) {
              file.closeAsync();
              reject(error instanceof Error ? error : new Error(String(error)));
            }
          });
        };

        for (let i = 0; i < sliceCount; i += 1) {
          getSlice(i);
        }
      }
    );
  });
}

async function sendToVerifier(docBytes: Uint8Array): Promise<ApiResponse> {
  const blob = new Blob([docBytes], {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  });

  const file = new File([blob], 'document.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  });

  const formData = new FormData();
  formData.append('document', file);

  const response = await fetch(`${BACKEND_URL}/api/verify`, {
    method: 'POST',
    body: formData
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Verifier returned ${response.status}: ${detail}`);
  }

  return response.json() as Promise<ApiResponse>;
}

async function handleVerify() {
  setStatus('Preparing document...');
  resetResults();

  try {
    if (!verifyButton) {
      verifyButton = document.getElementById('verify-document') as HTMLButtonElement | null;
    }
    if (verifyButton) {
      verifyButton.disabled = true;
    }

    const documentBytes = await getDocumentBytes();
    setStatus('Contacting verifier...');

    const result = await sendToVerifier(documentBytes);
    const citationCount = Array.isArray(result.citations) ? result.citations.length : 0;

    setStatus(`Verification complete: ${citationCount} citations returned.`);
    renderResults(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setStatus('Verification failed.');
    setResponse(message);
  }
  finally {
    if (verifyButton) {
      verifyButton.disabled = false;
    }
  }
}

Office.onReady((info) => {
  if (info.host !== Office.HostType.Word) {
    console.warn(`Unsupported host: ${info.host}`);
  }

  statusEl = document.getElementById('status');
  responseEl = document.getElementById('response');
  verifyButton = document.getElementById('verify-document') as HTMLButtonElement | null;
  resultContainer = document.getElementById('result-container');
  summaryEl = document.getElementById('result-summary');
  citationsEl = document.getElementById('citations');
  textPreviewEl = document.getElementById('text-preview');

  if (verifyButton) {
    verifyButton.addEventListener('click', () => {
      handleVerify();
    });
  } else {
    console.warn('Verify button not found in DOM.');
  }
});
