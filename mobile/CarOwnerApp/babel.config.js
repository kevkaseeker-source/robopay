module.exports = {
  presets: [
    [
      'module:metro-react-native-babel-preset',
      {unstable_transformProfile: 'hermes-stable'},
    ],
  ],
  // ethers v6's source uses private class fields/methods (#field syntax) -
  // RN 0.71's default Babel preset target doesn't enable these by default.
  plugins: [
    '@babel/plugin-proposal-private-methods',
    '@babel/plugin-proposal-class-properties',
  ],
};
