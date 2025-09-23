# Citation Verifier

This tool verifies legal citations in briefs, memos, and other court filings.

---

## Frontend

The frontend is a [Next.js](https://nextjs.org/) application.

### Installation

To install the frontend dependencies, run:

```bash
npm install
```

### Running the frontend

To run the frontend in development mode, run:

```bash
npm run dev
```

---

## Backend

The backend is a Python application.

### Installation

To install the backend dependencies, run:

```bash
pip install -r requirements.txt
```

### Running the backend

To run the backend, run:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
```

---

## Usage

1.  Upload a document (e.g., a `.docx` or `.pdf` file).
2.  The tool will extract the text from the document.
3.  The tool will then identify and verify the legal citations in the text.
4.  The results will be displayed to the user.

---

## License

AGPLv3 — see [LICENSE](LICENSE)

Note: `package.json` may list a different license string; the authoritative license for this repository is AGPLv3.

---

## Contributing

1. Fork the repo
2. Create a feature branch
3. Make changes (include tests where sensible)
4. Open a PR

---

## Contact

Questions or support: support@phaethon.llc