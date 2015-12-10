var fs = require('fs');
var os = require('os');
var spawn = require('child_process').spawn;
var path = require('path');
var request = require('request');

var PLATFORM = os.platform();
var ROOT = process.cwd();
var ARCH = 'arm';
var BUILD_WEBRTC = true ;
var NODEVER = process.version.split('.');

NODEVER[2] = 'x';
NODEVER = NODEVER.join('.');


function build() {
  console.log('Building module...');

  var nodegyp = path.resolve(ROOT, 'node_modules', 'node-gyp', 'bin', 'node-gyp.js');

  var res = spawn('node', [ nodegyp, 'rebuild' ], {
    cwd: ROOT,
    env: process.env,
    stdio: 'inherit',
  });

  res.on('error', function(error) {
    var res = spawn('iojs', [ nodegyp, 'rebuild' ], {
      cwd: ROOT,
      env: process.env,
      stdio: 'inherit',
    });
  });
}

function test() {
  console.log('Loading module...');

  try {
    var WebRTC = require(path.resolve(ROOT, 'build', 'Release', 'webrtc.node'));

    setTimeout(function() {
      console.log('Done! :)');
    }, 200);
  } catch (ignored) {
    if (PLATFORM == 'win32') {
      throw new Error('prebuilt module not working. See the instructions from https://github.com/vmolsa/webrtc-native#build-from-source for building module from source.');
    } else {
      throw new Error('prebuilt module not working. See the instructions from https://github.com/vmolsa/webrtc-native#build-from-source for building module from source.');
    }

    console.log('Install Failed! :(');
  }
}

build();

console.log('Downloading module...');

if (!fs.existsSync(path.resolve(ROOT, 'build', 'Release'))) {
  if (!fs.existsSync(path.resolve(ROOT, 'build'))) {
    fs.mkdirSync(path.resolve(ROOT, 'build'));
  }

  fs.mkdirSync(path.resolve(ROOT, 'build', 'Release'));
}

