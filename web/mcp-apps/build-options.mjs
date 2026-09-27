// Both the release builder and byte-preservation test use these exact options.
export const codeOptions = Object.freeze({
  target: 'es2022',
  minify: true,
  legalComments: 'eof',
  supported: { 'template-literal': false },
});
