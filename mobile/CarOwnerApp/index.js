/**
 * @format
 */
import {Buffer} from 'buffer';
import 'react-native-get-random-values';
import 'react-native-url-polyfill/auto';
import {TextDecoder, TextEncoder} from 'text-encoding';

import {AppRegistry} from 'react-native';
import {name as appName} from './app.json';

// Mock event listener functions to prevent them from fataling.
window.addEventListener = () => {};
window.removeEventListener = () => {};
window.Buffer = Buffer;

// @walletconnect/sign-client needs these - provided here directly instead
// of via @walletconnect/react-native-compat, whose bundled (and unused)
// WalletConnect-Pay native module pulls in a JNA version that fails to dex
// against this project's older Android toolchain.
if (typeof global.TextEncoder === 'undefined') {
  global.TextEncoder = TextEncoder;
}
if (typeof global.TextDecoder === 'undefined') {
  global.TextDecoder = TextDecoder;
}

// App is require()'d here, AFTER the polyfill assignments above, rather
// than imported at the top - a top-level `import App from './App'` compiles
// to a require() that Babel hoists to execute before these plain
// if-statements, so App (which pulls in @walletconnect/sign-client, which
// needs TextEncoder at its own module-load time) would load before the
// polyfill was actually in place, crashing the app before anything rendered.
const App = require('./App').default;

AppRegistry.registerComponent(appName, () => App);
