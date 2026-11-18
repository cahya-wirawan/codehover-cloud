function* ids() {
  let next = 1;
  while (true) yield next++;
}

const greet = function (name) {
  return "hello " + name;
};

module.exports = { ids, greet };
