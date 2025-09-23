declare module 'mammoth' {
  interface ConvertToHtmlOptions {
    buffer?: Buffer;
    path?: string;
    arrayBuffer?: ArrayBuffer;
  }

  interface ConvertToHtmlResult {
    value: string;
    messages: Array<{
      type: string;
      message: string;
    }>;
  }

  interface ConvertToMarkdownOptions {
    buffer?: Buffer;
    path?: string;
    arrayBuffer?: ArrayBuffer;
  }

  interface ConvertToMarkdownResult {
    value: string;
    messages: Array<{
      type: string;
      message: string;
    }>;
  }

  export function convertToHtml(options: ConvertToHtmlOptions): Promise<ConvertToHtmlResult>;
  export function convertToMarkdown(options: ConvertToMarkdownOptions): Promise<ConvertToMarkdownResult>;
}