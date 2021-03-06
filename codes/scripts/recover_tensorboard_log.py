from torch.utils.tensorboard import SummaryWriter

if __name__ == "__main__":
    writer = SummaryWriter("../experiments/recovered_tb")
    f = open("../experiments/recovered_tb.txt", encoding="utf8")
    console = f.readlines()
    search_terms = [
        ("iter", ", iter:  ", ", lr:"),
        ("l_g_total", " l_g_total: ", " switch_temperature:"),
        ("l_d_fake", "l_d_fake: ", " D_fake:")
    ]
    iter = 0
    for line in console:
        if " - INFO: [epoch:" not in line:
            continue
        for name, start, end in search_terms:
            val = line[line.find(start)+len(start):line.find(end)].replace(",", "")
            if name == "iter":
                iter = int(val)
            else:
                writer.add_scalar(name, float(val), iter)
    writer.close()